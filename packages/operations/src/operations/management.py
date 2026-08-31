from __future__ import annotations

import base64
import binascii
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from control.apps import AppService
from control.deployment_resources import (
    DeploymentResource,
    DeploymentResourceService,
    client_manifest_resource,
)
from control.deployments import CronJobService, DeploymentService
from control.service import ControlPlaneService, StubKind, StubRecord
from database.context import ServiceContext
from database.records.apps import AppRecord
from database.repositories.apps import (
    ActivityStartSource,
    AppRepository,
    AppSummaryRepository,
    DeploymentRepository,
)
from database.repositories.billing_costs import BillingLedgerCostRepository
from database.repositories.execution import (
    DetailedTaskRecord,
    LogRepository,
    RelatedTaskRecord,
    TaskDurationSample,
    TaskRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import (
    ContainerRepository,
)
from database.repositories.storage import ObjectRepository
from execution.containers.planning import validate_checkpoint_activation
from execution.containers.service import ContainerService
from execution.functions.service import FunctionControlService
from execution.tasks import TaskService
from observability.events import EventService
from observability.metrics import MetricsService
from observability.usage import UsageService
from pydantic import Field
from shared.billing_quotes import LedgerComponent
from shared.container_requests import (
    ContainerShutdownTarget,
    StopContainerReason,
    WorkerStartupKind,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.deployment_records import Deployment
from shared.deployments import DeploymentKind
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.client_manifests import INVOKABLE_DEPLOYMENT_KINDS, ClientManifestResource
from shared.http.observability import (
    ACTIVITY_MEASURE_UNITS,
    AccountActivityMeasure,
    AccountActivitySeriesKind,
    AccountActivityUnit,
    EventQueryResponse,
    LogObjectType,
    LogQueryResponse,
    LogRecord,
)
from shared.http.tasks import (
    TaskActionCapabilitiesResponse,
    TaskAppReferenceResponse,
    TaskDeploymentReferenceResponse,
    TaskWorkloadReferenceResponse,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import WorkspaceStatus
from shared.objects import ObjectRecord
from shared.realtime.streams import LogStreamQuery
from shared.tasks import Task, TaskStatus, is_terminal_task_status
from shared.timestamps import utc_now
from shared.worker_events import TASK_EVENT_RESOURCE_TYPE
from storage.service import ObjectStorage


class ManagementServices(Protocol):
    @property
    def context(self) -> ServiceContext: ...

    @property
    def apps(self) -> AppService: ...

    @property
    def deployment_resources(self) -> DeploymentResourceService: ...

    @property
    def deployments(self) -> DeploymentService: ...

    @property
    def cron_jobs(self) -> CronJobService: ...

    @property
    def containers(self) -> ContainerService: ...

    @property
    def events(self) -> EventService: ...

    @property
    def object_storage(self) -> ObjectStorage: ...

    @property
    def tasks(self) -> TaskService: ...

    # Cancelling a function task is the function service's decision to make, and
    # this is what that service needs to be built from here.
    @property
    def metrics(self) -> MetricsService: ...

    @property
    def usage(self) -> UsageService: ...


class CursorPage[T: ContractModel](ContractModel):
    data: tuple[T, ...]
    next: str = ""


class DeploymentCursorPayload(ContractModel):
    kind: str
    name: str
    version_order: int
    created_at: datetime
    deployment_id: str


class DeploymentUrlResult(ContractModel):
    deployment: Deployment
    stub: StubRecord | None = None
    url: str


class DeploymentPackagePlan(ContractModel):
    workspace_id: str
    stub_id: str
    object: ObjectRecord | None = None
    path: str | None = None
    presigned_url: str | None = None
    expires_in_seconds: int = 600
    filename: str = "package"


@dataclass(frozen=True, slots=True)
class PodDeploymentScaling:
    min_replicas: int
    max_replicas: int


class TaskCountByDeployment(ContractModel):
    deployment_id: str
    count: int
    status_counts: dict[TaskStatus, int] = Field(default_factory=dict)


class TaskTimeWindowBucket(ContractModel):
    timestamp: datetime
    count: int
    status_counts: dict[TaskStatus, int] = Field(default_factory=dict)


class TaskLatencyBucket(ContractModel):
    timestamp: datetime
    count: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    cold_starts: int = 0
    status_counts: dict[TaskStatus, int] = Field(default_factory=dict)


class TaskLatencyTimeseries(ContractModel):
    workspace_id: str
    stub_ids: tuple[str, ...] = ()
    deployment_id: str = ""
    window_seconds: int
    buckets: tuple[TaskLatencyBucket, ...] = ()


class TaskStopResult(ContractModel):
    stopped: tuple[str, ...]
    skipped: tuple[str, ...]


DEFAULT_TASK_WINDOW_BUCKETS = 48
"""How far back a task chart reads when its caller names no span.

A bound rather than a preference. Without one the query was the whole workspace's
task history on every refresh, which a dashboard left open turned into the most
expensive thing in the account. Forty-eight buckets is two days at the default
hourly width, and a caller that wants more says so.
"""


MAX_ACTIVITY_BUCKETS = 500
"""How many intervals one activity window may be cut into.

A ceiling rather than a preference: every interval is a point in every series a
reader gets back, so an unbounded one turns a chart request into a response
nothing can draw and a query nothing can serve.

Deliberately not the cost series' own interval cap. That one is derived from the
window a bill may cover, so a year of daily bars lands exactly on it; this window
has no such span to be measured against — its width is named in seconds by the
caller — so the figure here is only what a response can carry.
"""


class AccountContainerCounts(ContractModel):
    pending: int = 0
    running: int = 0


class AccountActivityBucket(ContractModel):
    timestamp: datetime
    value: float = 0.0


class AccountActivitySeries(ContractModel):
    kind: AccountActivitySeriesKind
    workspace_id: str = ""
    workspace_name: str = ""
    app_id: str = ""
    app_name: str = ""
    total: float = 0.0
    buckets: tuple[AccountActivityBucket, ...] = ()


class AccountActivity(ContractModel):
    measure: AccountActivityMeasure
    unit: AccountActivityUnit
    window_seconds: int
    start: datetime
    end: datetime
    total: float = 0.0
    series: tuple[AccountActivitySeries, ...] = ()


_START_SOURCES: Mapping[AccountActivityMeasure, ActivityStartSource] = {
    AccountActivityMeasure.Containers: ActivityStartSource.Containers,
    AccountActivityMeasure.Tasks: ActivityStartSource.Tasks,
}

_HELD_COMPONENTS: Mapping[AccountActivityMeasure, LedgerComponent] = {
    AccountActivityMeasure.Cpu: LedgerComponent.Cpu,
    AccountActivityMeasure.Memory: LedgerComponent.Memory,
    AccountActivityMeasure.Gpu: LedgerComponent.Gpu,
}
"""Which priced resource each held measure reads.

The ledger is the only place a workspace's processor, memory and card capacity is
recorded per app and per instant, and it is the record the customer is charged
from — so a chart drawn from anything else would be a second answer to what an
app used, differing from the invoice.
"""


class AppOperationalSummary(ContractModel):
    app: AppRecord
    latest_workload: StubRecord | None = None
    latest_deployment: Deployment | None = None
    workload_kinds: dict[str, int] = Field(default_factory=dict)
    workload_count: int = 0
    active_versions: int = 0
    running_containers: int = 0
    runs_24h: int = 0
    failed_runs_24h: int = 0
    pending_runs_24h: int = 0
    succeeded_runs_24h: int = 0
    activity_24h: tuple[int, ...] = ()
    failures_24h: tuple[int, ...] = ()
    pending_24h: tuple[int, ...] = ()
    succeeded_24h: tuple[int, ...] = ()
    last_deployed_at: datetime | None = None


class TaskView(ContractModel):
    """A task, the resources around it named, and what this reader may do to it.

    The task is a field rather than a base class, so passing one on re-reads
    nothing: it arrives validated from the repository and travels untouched into
    the response.
    """

    task: Task
    app: TaskAppReferenceResponse | None = None
    workload: TaskWorkloadReferenceResponse | None = None
    deployment: TaskDeploymentReferenceResponse | None = None
    actions: TaskActionCapabilitiesResponse = Field(default_factory=TaskActionCapabilitiesResponse)


class TaskDetailView(TaskView):
    """One task read on its own, which carries the container it ran in."""

    container: ContainerRecord | None = None


class TaskMetricsSummary(ContractModel):
    total: int
    status_counts: dict[TaskStatus, int] = Field(default_factory=dict)
    completed: int
    failed: int
    cancelled: int
    failure_rate: float = 0.0
    average_runtime_ms: float | None = None
    runtime_ms_p50: float | None = None
    runtime_ms_p95: float | None = None
    runtime_ms_p99: float | None = None
    startup_ms_p50: float | None = None
    startup_ms_p95: float | None = None


class ContainerStateWithApp(ContractModel):
    container: ContainerRecord
    app_id: str = ""


class ContainerActionCapabilities(ContractModel):
    can_stop: bool = False
    can_shell: bool = False
    can_create_image: bool = False
    can_snapshot_memory: bool = False


class ContainerView(ContainerRecord):
    app: AppRecord | None = None
    workload: StubRecord | None = None
    deployment: Deployment | None = None
    run_name: str | None = None
    run_status: TaskStatus | None = None
    expires_at: datetime | None = None
    actions: ContainerActionCapabilities = Field(default_factory=ContainerActionCapabilities)


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None or cursor == "":
        return 0
    try:
        value = int(cursor)
    except ValueError as exc:
        msg = f"invalid cursor: {cursor}"
        raise InvalidInputError(msg) from exc
    return max(value, 0)


type DeploymentCursor = tuple[str, str, int, datetime, str]


def _deployment_cursor_key(deployment: Deployment) -> DeploymentCursor:
    return (
        deployment.kind.value,
        deployment.name,
        -deployment.version,
        deployment.created_at,
        deployment.id,
    )


def _encode_deployment_cursor(cursor: DeploymentCursor) -> str:
    payload = DeploymentCursorPayload(
        kind=cursor[0],
        name=cursor[1],
        version_order=cursor[2],
        created_at=cursor[3],
        deployment_id=cursor[4],
    ).model_dump_json()
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_deployment_cursor(value: str | None) -> DeploymentCursor | None:
    if value is None or value == "":
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = DeploymentCursorPayload.model_validate_json(
            base64.urlsafe_b64decode(padded.encode())
        )
        return (
            payload.kind,
            payload.name,
            payload.version_order,
            payload.created_at,
            payload.deployment_id,
        )
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        msg = "invalid deployment cursor"
        raise InvalidInputError(msg) from exc


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _task_actions(
    record: RelatedTaskRecord,
    *,
    can_write: bool,
) -> TaskActionCapabilitiesResponse:
    terminal = is_terminal_task_status(record.task.status)
    return TaskActionCapabilitiesResponse(
        can_cancel=can_write and not terminal,
        can_rerun=can_write and terminal and record.workload_kind is StubKind.Function,
        can_shell=(
            can_write
            and record.container_status is ContainerStatus.Running
            and record.task.stub_id is not None
        ),
    )


def _task_app(record: RelatedTaskRecord) -> TaskAppReferenceResponse | None:
    if record.app_name is None:
        return None
    return TaskAppReferenceResponse(name=record.app_name)


def _task_workload(record: RelatedTaskRecord) -> TaskWorkloadReferenceResponse | None:
    if record.workload_name is None or record.workload_kind is None:
        return None
    return TaskWorkloadReferenceResponse(name=record.workload_name, kind=record.workload_kind)


def _task_deployment(record: RelatedTaskRecord) -> TaskDeploymentReferenceResponse | None:
    if record.deployment_name is None or record.deployment_version is None:
        return None
    return TaskDeploymentReferenceResponse(
        name=record.deployment_name,
        version=record.deployment_version,
    )


def _task_view(record: RelatedTaskRecord, *, can_write: bool) -> TaskView:
    return TaskView(
        task=record.task,
        app=_task_app(record),
        workload=_task_workload(record),
        deployment=_task_deployment(record),
        actions=_task_actions(record, can_write=can_write),
    )


def _task_detail_view(record: DetailedTaskRecord, *, can_write: bool) -> TaskDetailView:
    return TaskDetailView(
        task=record.task,
        app=_task_app(record),
        workload=_task_workload(record),
        deployment=_task_deployment(record),
        actions=_task_actions(record, can_write=can_write),
        container=record.container,
    )


def _log_object_type(value: str | None) -> LogObjectType | None:
    if not value:
        return None
    try:
        return LogObjectType(value.strip().lower().replace("-", "_").replace("_", ""))
    except ValueError:
        return None


def _local_package_path(path: str) -> Path | None:
    if path.startswith("file://"):
        return Path(path.removeprefix("file://")).expanduser().resolve()
    if "://" in path:
        return None
    return Path(path).expanduser().resolve()


def _account_activity_series(
    amounts: Mapping[tuple[str, str], list[float]],
    *,
    workspace_names: Mapping[str, str],
    app_names: Mapping[str, str],
    start: datetime,
    window_seconds: int,
    bucket_divisors: Sequence[float],
    window_divisor: float,
    limit: int,
) -> tuple[AccountActivitySeries, ...]:
    """Dense per-app readings, capped at what a reader was asked for.

    Everything past the cap is summed into one `Other` series rather than
    dropped, so the stacks a reader sees still add up to the window they are told
    they are looking at.

    Amounts arrive undivided, and the window total divides their sum by the whole
    span rather than adding up the intervals' own levels: a level already divided
    once by the seconds its interval covers cannot be divided again, and averaging
    the intervals as equals would weight the one in progress like a whole hour.
    """

    ordered = sorted(
        amounts.items(),
        key=lambda entry: (
            -sum(entry[1]),
            app_names.get(entry[0][1], ""),
            entry[0][0],
            entry[0][1],
        ),
    )
    series = [
        AccountActivitySeries(
            kind=(
                AccountActivitySeriesKind.App if app_id else AccountActivitySeriesKind.Unassigned
            ),
            workspace_id=workspace_id,
            workspace_name=workspace_names.get(workspace_id, ""),
            app_id=app_id,
            app_name=app_names.get(app_id, ""),
            total=sum(readings) / window_divisor,
            buckets=_activity_buckets(readings, start, window_seconds, bucket_divisors),
        )
        for (workspace_id, app_id), readings in ordered[:limit]
    ]
    folded = ordered[limit:]
    if folded:
        columns = zip(*(readings for _, readings in folded), strict=True)
        summed = [sum(readings) for readings in columns]
        series.append(
            AccountActivitySeries(
                kind=AccountActivitySeriesKind.Other,
                total=sum(summed) / window_divisor,
                buckets=_activity_buckets(summed, start, window_seconds, bucket_divisors),
            )
        )
    return tuple(series)


def _activity_buckets(
    amounts: Sequence[float],
    start: datetime,
    window_seconds: int,
    divisors: Sequence[float],
) -> tuple[AccountActivityBucket, ...]:
    """One reading per interval, each divided by the span it stands for.

    A divisor of zero is an interval nothing can have happened in yet, so it
    reads as zero rather than as an amount over no time at all.
    """

    return tuple(
        AccountActivityBucket(
            timestamp=start + timedelta(seconds=window_seconds * index),
            value=amount / divisors[index] if divisors[index] > 0 else 0.0,
        )
        for index, amount in enumerate(amounts)
    )


def _covered_seconds(
    *,
    start: datetime,
    measured_through: datetime,
    window_seconds: int,
    bucket_count: int,
) -> tuple[float, ...]:
    """How many seconds of each interval the window actually covers.

    Every interval but the last covers its whole width. The last one is the
    interval in progress: dividing a level by the width of an interval only part
    of which has happened reads the newest point — the one somebody opens this to
    look at — at a fraction of the level actually held.
    """

    measured = (measured_through - start).total_seconds()
    return tuple(
        min(max(measured - window_seconds * index, 0.0), float(window_seconds))
        for index in range(bucket_count)
    )


def _bucket_start(value: datetime, window_seconds: int) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=value.tzinfo)
    elapsed = int((value - epoch).total_seconds())
    bucket_elapsed = elapsed - (elapsed % window_seconds)
    return epoch + timedelta(seconds=bucket_elapsed)


def _runtime_ms(task: Task) -> float | None:
    if task.started_at is None or task.finished_at is None:
        return None
    return (task.finished_at - task.started_at).total_seconds() * 1000


def _startup_ms(task: Task) -> float | None:
    if task.started_at is None:
        return None
    return (task.started_at - task.created_at).total_seconds() * 1000


def _sample_runtime_ms(sample: TaskDurationSample) -> float:
    return (sample.finished_at - sample.started_at).total_seconds() * 1000


def _sample_startup_ms(sample: TaskDurationSample) -> float:
    return (sample.started_at - sample.created_at).total_seconds() * 1000


def _container_expires_at(
    container: ContainerRecord,
    workload: StubRecord | None,
) -> datetime | None:
    if workload is None or workload.kind is not StubKind.Sandbox:
        return None
    try:
        lifetime_seconds = int(container.env.get("KEEP_WARM_SECONDS", "0"))
    except ValueError:
        return None
    if lifetime_seconds <= 0:
        return None
    return (container.started_at or container.created_at) + timedelta(seconds=lifetime_seconds)


def _percentile(sorted_values: list[float], quantile: float) -> float | None:
    """Linear-interpolated percentile over an ascending-sorted sample."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = quantile * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


@dataclass
class ManagementService:
    services: ManagementServices

    @property
    def control_plane(self) -> ControlPlaneService:
        return ControlPlaneService(
            self.services.context,
            workspace_changes=self.services.deployments.workspace_changes,
        )

    def list_deployments(
        self,
        workspace: str,
        *,
        active: bool | None = None,
        app_id: str | None = None,
        name: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> CursorPage[Deployment]:
        deployments = [
            resource.deployment
            for resource in self.services.deployment_resources.list(
                workspace=workspace,
                app_id=app_id,
                name=name,
                active=active,
            )
        ]
        deployments.sort(key=_deployment_cursor_key)
        decoded_cursor = _decode_deployment_cursor(cursor)
        if decoded_cursor is not None:
            deployments = [
                deployment
                for deployment in deployments
                if _deployment_cursor_key(deployment) > decoded_cursor
            ]
        page = deployments[:limit]
        next_cursor = (
            _encode_deployment_cursor(_deployment_cursor_key(page[-1]))
            if len(deployments) > limit and page
            else ""
        )
        return CursorPage(
            data=tuple(page),
            next=next_cursor,
        )

    def latest_deployments(
        self,
        workspace: str,
        *,
        app_id: str | None = None,
        name: str | None = None,
        limit: int = 100,
    ) -> CursorPage[Deployment]:
        grouped: dict[tuple[str, str], Deployment] = {}
        for resource in self.services.deployment_resources.list(
            workspace=workspace,
            app_id=app_id,
            name=name,
            active=True,
        ):
            deployment = resource.deployment
            key = (deployment.kind.value, deployment.name)
            existing = grouped.get(key)
            if existing is None or deployment.version > existing.version:
                grouped[key] = deployment
        latest = sorted(grouped.values(), key=lambda item: (item.name, item.version))
        return CursorPage(data=tuple(latest[:limit]))

    def app_summaries(self, workspace: str) -> tuple[AppOperationalSummary, ...]:
        workspace_record = self.control_plane.get_workspace(workspace)
        apps = self.services.apps.list(workspace=workspace_record.id, active=None)
        resources = self.services.deployment_resources.list(
            workspace=workspace_record.id,
            active=None,
        )
        resources_by_app: dict[str, list[DeploymentResource]] = {}
        for resource in resources:
            resources_by_app.setdefault(resource.app.id, []).append(resource)

        hour_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        with self.services.context.database.session() as session:
            execution = AppSummaryRepository(session).execution_summaries(
                workspace_id=workspace_record.id,
                start=hour_start,
            )

        summaries: list[AppOperationalSummary] = []
        for app in apps:
            app_resources = resources_by_app.get(app.id, [])
            latest = max(
                app_resources,
                key=lambda item: (item.deployment.created_at, item.deployment.version),
                default=None,
            )
            workloads = {
                (resource.stub.kind.value, resource.stub.name) for resource in app_resources
            }
            workload_kinds = Counter(kind for kind, _name in workloads)
            facts = execution.get(app.id)
            summaries.append(
                AppOperationalSummary(
                    app=app,
                    latest_workload=latest.stub if latest is not None else None,
                    latest_deployment=latest.deployment if latest is not None else None,
                    workload_kinds=dict(sorted(workload_kinds.items())),
                    workload_count=len(workloads),
                    active_versions=sum(
                        1 for resource in app_resources if resource.deployment.active
                    ),
                    running_containers=facts.running_containers if facts is not None else 0,
                    runs_24h=facts.runs_24h if facts is not None else 0,
                    failed_runs_24h=facts.failed_runs_24h if facts is not None else 0,
                    pending_runs_24h=facts.pending_runs_24h if facts is not None else 0,
                    succeeded_runs_24h=facts.succeeded_runs_24h if facts is not None else 0,
                    activity_24h=(tuple(facts.activity_24h) if facts is not None else (0,) * 24),
                    failures_24h=(tuple(facts.failures_24h) if facts is not None else (0,) * 24),
                    pending_24h=(tuple(facts.pending_24h) if facts is not None else (0,) * 24),
                    succeeded_24h=(tuple(facts.succeeded_24h) if facts is not None else (0,) * 24),
                    last_deployed_at=(latest.deployment.created_at if latest is not None else None),
                )
            )
        summaries.sort(key=lambda item: item.app.name)
        return tuple(summaries)

    def retrieve_deployment(self, workspace: str, deployment_id_or_name: str) -> Deployment:
        if _is_uuid(deployment_id_or_name):
            resource = self.services.deployment_resources.get_by_deployment_id(
                deployment_id_or_name,
                workspace=workspace,
            )
            if resource is not None:
                return resource.deployment
        matches = [
            resource.deployment
            for resource in self.services.deployment_resources.list(
                workspace=workspace,
                name=deployment_id_or_name,
                active=None,
            )
        ]
        if matches:
            return max(matches, key=lambda item: item.version)
        msg = f"deployment not found in workspace: {deployment_id_or_name}"
        raise NotFoundError(msg)

    def set_deployment_active(
        self,
        workspace: str,
        deployment_id_or_name: str,
        *,
        active: bool,
        allow_paused_app: bool = False,
    ) -> Deployment:
        deployment = self.retrieve_deployment(workspace, deployment_id_or_name)
        workspace_id = self.control_plane.get_workspace(workspace).id
        if active and deployment.app_id and not allow_paused_app:
            app = self.services.apps.get(deployment.app_id, workspace=workspace)
            if not app.active:
                msg = f"cannot start deployment while app is paused: {app.name}"
                raise ConflictError(msg)
        deployment.active = active
        deployment.updated_at = utc_now()
        with self.services.context.database.session() as session:
            updated = DeploymentRepository(session).records.upsert(
                deployment,
                workspace_id=workspace_id,
                name=deployment.name,
                status="active" if deployment.active else "inactive",
            )
        self._publish_deployment_change(updated, workspace_id=workspace_id)
        # Unconditional: the call matches on deployment id, so a deployment with
        # no schedule has nothing to toggle and asking is cheaper than knowing.
        self.services.cron_jobs.set_deployment_enabled(
            updated.id,
            enabled=active,
            workspace=workspace,
        )
        if not active:
            self._stop_deployment_containers(workspace, updated)
        self.services.events.emit(
            "deployment.started" if active else "deployment.stopped",
            resource_type="deployment",
            resource_id=updated.id,
            message=f"{'started' if active else 'stopped'} deployment {updated.name}",
            workspace_id=workspace_id,
        )
        return updated

    def scale_deployment(
        self,
        workspace: str,
        deployment_id_or_name: str,
        *,
        containers: int,
    ) -> Deployment:
        if containers < 0:
            raise InvalidInputError("replicas cannot be negative")
        deployment = self.retrieve_deployment(workspace, deployment_id_or_name)
        if deployment.kind is not DeploymentKind.Pod:
            raise InvalidInputError("only pod deployments can be scaled directly")
        if not deployment.active:
            raise ConflictError(f"cannot scale inactive deployment: {deployment.name}")
        if deployment.app_id:
            app = self.services.apps.get(deployment.app_id, workspace=workspace)
            if not app.active:
                raise ConflictError(f"cannot scale deployment while app is paused: {app.name}")
        scaled_stubs = self._scale_pod_deployment_stubs(workspace, deployment, containers)
        if not scaled_stubs:
            raise ConflictError(f"pod deployment has no scalable workload: {deployment.name}")
        deployment.updated_at = utc_now()
        with self.services.context.database.session() as session:
            updated = DeploymentRepository(session).records.upsert(
                deployment,
                workspace_id=scaled_stubs[0].workspace_id,
                name=deployment.name,
                status="active" if deployment.active else "inactive",
            )
        self._publish_deployment_change(
            updated,
            workspace_id=scaled_stubs[0].workspace_id,
        )
        self.services.events.emit(
            "deployment.scale.updated",
            resource_type="deployment",
            resource_id=deployment.id,
            message=f"scaled deployment {deployment.name} to {containers} containers",
            data={
                "containers": containers,
                "stub_ids": [stub.id for stub in scaled_stubs],
            },
            workspace_id=scaled_stubs[0].workspace_id,
        )
        return updated

    def pod_deployment_scaling(
        self,
        workspace: str,
        deployment_ids: set[str],
    ) -> dict[str, PodDeploymentScaling]:
        if not deployment_ids:
            return {}
        workspace_record = self.control_plane.get_workspace(workspace)
        grouped: dict[str, list[StubRecord]] = {}
        for stub in self.control_plane.list_stubs(workspace=workspace_record.id):
            if stub.deployment_id not in deployment_ids:
                continue
            grouped.setdefault(stub.deployment_id, []).append(stub)

        scaling: dict[str, PodDeploymentScaling] = {}
        for deployment_id, stubs in grouped.items():
            if any(stub.kind is not StubKind.Pod for stub in stubs):
                continue
            scaling[deployment_id] = PodDeploymentScaling(
                min_replicas=min(stub.config.autoscaler.min_containers for stub in stubs),
                max_replicas=max(stub.config.autoscaler.max_containers for stub in stubs),
            )
        return scaling

    def delete_deployment(self, workspace: str, deployment_id_or_name: str) -> Deployment:
        deployment = self.retrieve_deployment(workspace, deployment_id_or_name)
        self._stop_deployment_containers(workspace, deployment)
        return self.services.deployments.delete(deployment.id)

    def stop_all_active_deployments(self, workspace: str) -> tuple[Deployment, ...]:
        active = [
            Deployment.model_validate(item)
            for item in self.list_deployments(workspace, active=True, limit=10_000).data
        ]
        return tuple(
            self.set_deployment_active(workspace, deployment.id, active=False)
            for deployment in active
        )

    def stop_all_active_deployments_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> tuple[Deployment, ...]:
        """Deactivate existing deployments without reopening tenant admission."""
        now = utc_now()
        with self.services.context.database.session() as session:
            repository = DeploymentRepository(session)
            active = repository.list(workspace_id=workspace_id, active=True)
            stopped = [
                updated
                for deployment in active
                if (
                    updated := repository.deactivate_for_workspace_deletion(
                        deployment.id,
                        workspace_id=workspace_id,
                        now=now,
                    )
                )
                is not None
            ]
        return tuple(stopped)

    def _stop_deployment_containers(self, workspace: str, deployment: Deployment) -> None:
        workspace_record = self.control_plane.get_workspace(workspace)
        stub_ids = {
            stub.id
            for stub in self.control_plane.list_stubs(workspace=workspace_record.id)
            if stub.deployment_id == deployment.id
        }
        if not stub_ids:
            return
        active_statuses = {ContainerStatus.Pending, ContainerStatus.Running}
        for container in self.services.containers.list():
            if (
                container.workspace_id == workspace_record.id
                and container.stub_id in stub_ids
                and container.status in active_statuses
            ):
                self.services.containers.stop(container.id)

    def _scale_pod_deployment_stubs(
        self,
        workspace: str,
        deployment: Deployment,
        containers: int,
    ) -> list[StubRecord]:
        workspace_record = self.control_plane.get_workspace(workspace)
        stubs = [
            stub
            for stub in self.control_plane.list_stubs(workspace=workspace_record.id)
            if stub.deployment_id == deployment.id
        ]
        if not stubs or any(stub.kind is not StubKind.Pod for stub in stubs):
            return []
        if containers == 0 and any(stub.config.runtime.keep_warm == -1 for stub in stubs):
            raise InvalidInputError("always-on pod deployments cannot be scaled to zero")
        if containers > 0:
            for stub in stubs:
                validate_checkpoint_activation(
                    startup_kind=WorkerStartupKind.Pod,
                    runtime=stub.config.runtime,
                )
        updated: list[StubRecord] = []
        for stub in stubs:
            autoscaler = stub.config.autoscaler.model_copy(
                update={
                    "max_containers": containers,
                    "min_containers": containers,
                }
            )
            result = self.control_plane.update_stub_config(
                stub.id,
                workspace=workspace_record.id,
                fields={"autoscaler": autoscaler},
            )
            updated.append(result.stub)
        return updated

    def _publish_deployment_change(
        self,
        deployment: Deployment,
        *,
        workspace_id: str,
    ) -> None:
        publisher = self.services.deployments.workspace_changes
        if publisher is None:
            return
        publisher.emit_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.Deployments,
            change=WorkspaceChangeType.Updated,
            resource_id=deployment.id,
            app_id=deployment.app_id,
            deployment_id=deployment.id,
        )

    def deployment_url(
        self,
        deployment_id_or_name: str,
        *,
        workspace: str | None = None,
        external_url: str = "http://127.0.0.1:9000",
    ) -> DeploymentUrlResult:
        deployment = (
            self.retrieve_deployment(workspace, deployment_id_or_name)
            if workspace
            else self.services.deployments.get(deployment_id_or_name)
        )
        resource = self.services.deployment_resources.get_by_deployment_id(
            deployment.id,
            workspace=workspace,
        )
        if resource is None:
            msg = f"deployment resource not found: {deployment.id}"
            raise NotFoundError(msg)
        return DeploymentUrlResult(
            deployment=resource.deployment,
            stub=resource.stub,
            url=resource.invoke_url(external_url),
        )

    def deployment_manifest(
        self,
        deployment_id: str,
        *,
        workspace: str,
        external_url: str = "http://127.0.0.1:9000",
    ) -> ClientManifestResource:
        """Invoke manifest (URL, schemas, client contract) for one deployment."""
        deployment = self.retrieve_deployment(workspace, deployment_id)
        if deployment.kind not in INVOKABLE_DEPLOYMENT_KINDS:
            msg = f"deployment kind is not invokable: {deployment.kind.value}"
            raise InvalidInputError(msg)
        resource = self.services.deployment_resources.get_by_deployment_id(
            deployment.id,
            workspace=workspace,
        )
        if resource is None:
            msg = f"deployment resource not found: {deployment.id}"
            raise NotFoundError(msg)
        return client_manifest_resource(resource, external_url=external_url)

    def deployment_url_by_name(
        self,
        workspace: str,
        stub_type: StubKind,
        deployment_name: str,
        version: int | None = None,
        *,
        app_id: str | None = None,
        external_url: str = "http://127.0.0.1:9000",
    ) -> DeploymentUrlResult:
        try:
            deployment_kind = DeploymentKind(stub_type.value)
        except ValueError as exc:
            msg = f"deployment kind is not invokable: {stub_type}"
            raise InvalidInputError(msg) from exc
        resource = self.services.deployment_resources.resolve_invoke_target(
            deployment_name,
            deployment_kind,
            workspace=workspace,
            version=version,
            app_id=app_id,
        )
        return DeploymentUrlResult(
            deployment=resource.deployment,
            stub=resource.stub,
            # A caller that named a version gets a URL that keeps pointing at it.
            url=resource.invoke_url(external_url, pin_version=version is not None),
        )

    def deployment_package(self, workspace: str, stub_id: str) -> DeploymentPackagePlan:
        workspace_record = self.control_plane.get_workspace(workspace)
        with self.services.context.database.session() as session:
            objects = ObjectRepository(session).list(workspace_id=workspace_record.id)
        chosen = next(
            (
                item
                for item in objects
                if item.metadata.get("stub_id") == stub_id
                or (
                    item.metadata.get("workspace_id") == workspace_record.id
                    and item.key.endswith(stub_id)
                )
            ),
            None,
        )
        if chosen is None:
            return DeploymentPackagePlan(workspace_id=workspace_record.id, stub_id=stub_id)
        local_path = _local_package_path(chosen.path)
        if local_path is not None and local_path.is_file():
            return DeploymentPackagePlan(
                workspace_id=workspace_record.id,
                stub_id=stub_id,
                object=chosen,
                path=str(local_path),
                filename=Path(chosen.key).name or f"{stub_id}.bin",
            )
        presigned_url = self.services.object_storage.generate_presigned_get_url_for_workspace(
            workspace_id=workspace_record.id,
            bucket=chosen.bucket,
            key=chosen.key,
            expires_seconds=600,
        )
        return DeploymentPackagePlan(
            workspace_id=workspace_record.id,
            stub_id=stub_id,
            object=chosen,
            path=None,
            presigned_url=presigned_url,
            filename=Path(chosen.key).name or f"{stub_id}.bin",
        )

    def task_page(
        self,
        workspace: str,
        *,
        status: TaskStatus | None = None,
        deployment_id: str | None = None,
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
        kind: StubKind | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        search: str | None = None,
        root_only: bool = False,
        can_write: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CursorPage[TaskView]:
        workspace_record = self.control_plane.get_workspace(workspace)
        offset = _parse_cursor(cursor)
        with self.services.context.database.session() as session:
            page = TaskRepository(session).page_with_related(
                workspace_id=workspace_record.id,
                status=status,
                deployment_id=deployment_id,
                app_id=app_id,
                stub_ids=stub_ids,
                kind=kind,
                created_after=created_after,
                created_before=created_before,
                search=search,
                root_only=root_only,
                limit=limit,
                offset=offset,
            )
        return CursorPage(
            data=tuple(_task_view(item, can_write=can_write) for item in page.data),
            next=page.next,
        )

    def task_detail(
        self,
        workspace: str,
        task_id: str,
        *,
        can_write: bool = False,
    ) -> TaskDetailView:
        workspace_record = self.control_plane.get_workspace(workspace)
        with self.services.context.database.session() as session:
            related = TaskRepository(session).get_with_related(
                task_id,
                workspace_id=workspace_record.id,
            )
        if related is None:
            msg = f"task not found in workspace: {task_id}"
            raise NotFoundError(msg)
        return _task_detail_view(related, can_write=can_write)

    def workspace_task(self, workspace: str, task_id: str) -> Task:
        return self.task_detail(workspace, task_id).task

    def task_counts_by_deployment(self, workspace: str) -> tuple[TaskCountByDeployment, ...]:
        workspace_record = self.control_plane.get_workspace(workspace)
        with self.services.context.database.session() as session:
            tallies = TaskRepository(session).status_tallies_by_deployment(
                workspace_id=workspace_record.id
            )
        counts: dict[str, Counter[TaskStatus]] = {}
        for tally in tallies:
            counts.setdefault(tally.deployment_id or "", Counter())[tally.status] += tally.count
        return tuple(
            TaskCountByDeployment(
                deployment_id=deployment_id,
                count=sum(counter.values()),
                status_counts=dict(counter),
            )
            for deployment_id, counter in sorted(counts.items())
        )

    def aggregate_tasks_by_time_window(
        self,
        workspace: str,
        *,
        window_seconds: int = 3600,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        app_id: str | None = None,
        stub_id: str | None = None,
    ) -> tuple[TaskTimeWindowBucket, ...]:
        """Task counts per time bucket, over a bounded span.

        The span is bounded even when the caller names none. A chart draws a
        fixed number of buckets, so reading further back than it can draw is
        work nobody sees, and this asked for every task in the workspace on
        every refresh until it was given an end.
        """
        if window_seconds <= 0:
            msg = "window_seconds must be greater than zero"
            raise InvalidInputError(msg)
        end = ended_at or utc_now()
        start = started_at or end - timedelta(seconds=window_seconds * DEFAULT_TASK_WINDOW_BUCKETS)
        workspace_record = self.control_plane.get_workspace(workspace)
        with self.services.context.database.session() as session:
            samples = TaskRepository(session).creation_samples(
                workspace_id=workspace_record.id,
                start=start,
                end=end,
                app_id=app_id,
                stub_id=stub_id,
            )
        buckets: dict[datetime, Counter[TaskStatus]] = {}
        for sample in samples:
            bucket = _bucket_start(sample.created_at, window_seconds)
            buckets.setdefault(bucket, Counter())[sample.status] += 1
        return tuple(
            TaskTimeWindowBucket(
                timestamp=timestamp,
                count=sum(counter.values()),
                status_counts=dict(counter),
            )
            for timestamp, counter in sorted(buckets.items())
        )

    def account_container_counts(self, *, workspace_ids: Sequence[str]) -> AccountContainerCounts:
        """What this account is holding right now, per live status.

        Account-scoped on purpose, and the caller resolves the set from
        membership: the concurrency ceiling somebody is refused against spans an
        account, so a figure covering one workspace would be read against a limit
        it is not counted for.
        """

        with self.services.context.database.session() as session:
            counts = ContainerRepository(session).live_counts_for_workspaces(
                workspace_ids=workspace_ids
            )
        return AccountContainerCounts(
            pending=counts.get(ContainerStatus.Pending, 0),
            running=counts.get(ContainerStatus.Running, 0),
        )

    def account_activity(
        self,
        *,
        workspaces: Mapping[str, str],
        measure: AccountActivityMeasure = AccountActivityMeasure.Containers,
        window_seconds: int = 3600,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5,
    ) -> AccountActivity:
        """An account's activity over a window, split by the app it belongs to.

        `workspaces` maps every workspace the caller reaches to its name, and is
        the whole scope of the answer — an account-wide reading assembled from
        ids a request named would be a reading of whatever it asked for.

        Both ends are aligned to interval boundaries so the same wall-clock
        intervals come back on every read: unaligned, every refresh would slide
        the window by however long the last one took and redraw a chart that had
        not changed.

        Series are densified over the whole window here rather than in the
        reader, because an interval an account started nothing in is a real zero
        and a reader filling one in has no way to tell it from an interval nobody
        measured. A resource nothing was placed on reads the same way: a flat
        zero band is the true answer, not an absent one.

        A start is counted; a resource is a level, summed in the unit it was
        priced in and divided by the seconds its interval covers. The two never
        share a figure, and `unit` on the result is what says which one this is.
        """

        if window_seconds <= 0:
            msg = "window_seconds must be greater than zero"
            raise InvalidInputError(msg)
        if limit <= 0:
            msg = "limit must be greater than zero"
            raise InvalidInputError(msg)
        now = utc_now()
        aligned_end = _bucket_start(end or now, window_seconds) + timedelta(seconds=window_seconds)
        aligned_start = _bucket_start(
            start if start is not None else aligned_end - timedelta(seconds=window_seconds * 24),
            window_seconds,
        )
        if aligned_start >= aligned_end:
            msg = "the activity window must end after it starts"
            raise InvalidInputError(msg)
        bucket_count = int((aligned_end - aligned_start).total_seconds()) // window_seconds
        if bucket_count > MAX_ACTIVITY_BUCKETS:
            msg = (
                f"an activity window holds at most {MAX_ACTIVITY_BUCKETS} intervals; "
                f"this one asks for {bucket_count}"
            )
            raise InvalidInputError(msg)

        unit = ACTIVITY_MEASURE_UNITS[measure]
        counted = unit is AccountActivityUnit.Starts
        workspace_ids = sorted(workspaces)
        measured_through = min(aligned_end, now)
        # A count belongs to its interval whole and is divided by nothing. A
        # level is an amount over a span, so every reading of one is divided by
        # the span it stands for: its interval, and for the window total the
        # whole of it.
        bucket_divisors = (
            (1.0,) * bucket_count
            if counted
            else _covered_seconds(
                start=aligned_start,
                measured_through=measured_through,
                window_seconds=window_seconds,
                bucket_count=bucket_count,
            )
        )
        window_divisor = (
            1.0 if counted else max((measured_through - aligned_start).total_seconds(), 1.0)
        )

        amounts: dict[tuple[str, str], list[float]] = {}
        with self.services.context.database.session() as session:
            # Two tables answer this, and which one is not a detail of the chart:
            # a start is a row in the orchestration record, and a resource is a
            # priced segment of the ledger. Both come back keyed by workspace and
            # app together, because two workspaces may hold apps of the same name
            # and neither may be merged into the other's band.
            source = _START_SOURCES.get(measure)
            readings: tuple[tuple[tuple[str, str], int, float], ...] = (
                tuple(
                    ((row.workspace_id, row.app_id or ""), row.index, float(row.count))
                    for row in AppSummaryRepository(session).activity_by_app(
                        workspace_ids=workspace_ids,
                        source=source,
                        start=aligned_start,
                        end=aligned_end,
                        window_seconds=window_seconds,
                    )
                )
                if source is not None
                else tuple(
                    ((held.workspace_id, held.app_id), held.index, float(held.quantity))
                    for held in BillingLedgerCostRepository(session).component_bucket_quantities(
                        workspace_ids=workspace_ids,
                        component=_HELD_COMPONENTS[measure],
                        start=aligned_start,
                        end=aligned_end,
                        width_seconds=window_seconds,
                    )
                )
            )
            for key, index, amount in readings:
                if index < 0 or index >= bucket_count:
                    continue
                amounts.setdefault(key, [0.0] * bucket_count)[index] += amount
            app_names = AppRepository(session).names([app_id for _, app_id in amounts if app_id])
        return AccountActivity(
            measure=measure,
            unit=unit,
            window_seconds=window_seconds,
            start=aligned_start,
            end=aligned_end,
            total=sum(sum(series) for series in amounts.values()) / window_divisor,
            series=_account_activity_series(
                amounts,
                workspace_names=workspaces,
                app_names=app_names,
                start=aligned_start,
                window_seconds=window_seconds,
                bucket_divisors=bucket_divisors,
                window_divisor=window_divisor,
                limit=limit,
            ),
        )

    def task_latency_timeseries(
        self,
        workspace: str,
        *,
        stub_ids: tuple[str, ...],
        deployment_id: str | None = None,
        window_seconds: int = 3600,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> TaskLatencyTimeseries:
        """Per-stub task-duration percentiles plus cold starts, bucketed over time.

        Durations use the same rule as `task_metrics`: any task with both
        `started_at` and `finished_at`. Cold starts count container creations
        for the stubs. Reads are SQL-windowed; only the bounded window is
        bucketed here.
        """
        if window_seconds <= 0:
            msg = "window_seconds must be greater than zero"
            raise InvalidInputError(msg)
        if not stub_ids:
            msg = "at least one stub_id is required"
            raise InvalidInputError(msg)
        workspace_record = self.control_plane.get_workspace(workspace)
        resolved_end = end or utc_now()
        resolved_start = start or resolved_end - timedelta(hours=24)
        with self.services.context.database.session() as session:
            samples = TaskRepository(session).duration_samples(
                workspace_id=workspace_record.id,
                stub_ids=stub_ids,
                deployment_id=deployment_id,
                start=resolved_start,
                end=resolved_end,
            )
            cold_start_times = ContainerRepository(session).creation_times(
                workspace_id=workspace_record.id,
                stub_ids=stub_ids,
                start=resolved_start,
                end=resolved_end,
            )
        durations: dict[datetime, list[float]] = {}
        statuses: dict[datetime, Counter[TaskStatus]] = {}
        for sample in samples:
            bucket = _bucket_start(sample.created_at, window_seconds)
            durations.setdefault(bucket, []).append(
                (sample.finished_at - sample.started_at).total_seconds() * 1000
            )
            statuses.setdefault(bucket, Counter())[sample.status] += 1
        cold_starts = Counter(
            _bucket_start(created_at, window_seconds) for created_at in cold_start_times
        )
        buckets: list[TaskLatencyBucket] = []
        for timestamp in sorted({*durations, *cold_starts}):
            values = sorted(durations.get(timestamp, []))
            buckets.append(
                TaskLatencyBucket(
                    timestamp=timestamp,
                    count=len(values),
                    p50_ms=_percentile(values, 0.50),
                    p95_ms=_percentile(values, 0.95),
                    cold_starts=cold_starts.get(timestamp, 0),
                    status_counts=dict(statuses.get(timestamp, Counter())),
                )
            )
        return TaskLatencyTimeseries(
            workspace_id=workspace_record.id,
            stub_ids=stub_ids,
            deployment_id=deployment_id or "",
            window_seconds=window_seconds,
            buckets=tuple(buckets),
        )

    def stop_tasks(self, workspace: str, task_ids: list[str]) -> TaskStopResult:
        workspace_record = self.control_plane.get_workspace(workspace)
        with self.services.context.database.session() as session:
            workspace_task_ids = TaskRepository(session).existing_ids(
                workspace_id=workspace_record.id,
                task_ids=task_ids,
            )
        stopped: list[str] = []
        skipped: list[str] = []
        for task_id in task_ids:
            if task_id not in workspace_task_ids:
                skipped.append(task_id)
                continue
            task = self.services.tasks.get(task_id)
            if is_terminal_task_status(task.status):
                skipped.append(task_id)
                continue
            self._cancel_task(task)
            stopped.append(task_id)
        return TaskStopResult(stopped=tuple(stopped), skipped=tuple(skipped))

    def _cancel_task(self, task: Task) -> None:
        """Cancel one task through whatever owns stopping its kind of work.

        Nothing inside a container watches the task row, so writing `cancelled`
        on it stops no work. Reaching the handler means going through the service
        that knows what stopping this workload does to the invocations beside
        it.

        Built here the way this service builds its control plane, and safe to
        build without a gateway origin because cancelling only settles work: it
        stops a container and fails what was waiting on the cancelled call.
        Nothing on this path starts a container, which is the one thing that
        would need to tell a container where to call back.
        """

        stub = self._stub_for_task(task)
        if stub is not None and stub.kind is StubKind.Function:
            FunctionControlService(self.services).cancel_task(task.id)
            return
        self.services.tasks.cancel(task.id)

    def _stub_for_task(self, task: Task) -> StubRecord | None:
        if not task.stub_id:
            return None
        try:
            return self.control_plane.get_stub(task.stub_id)
        except NotFoundError:
            return None

    def task_metrics(
        self,
        *,
        workspace: str,
        started_at: datetime,
        ended_at: datetime,
        app_id: str | None = None,
    ) -> TaskMetricsSummary:
        workspace_record = self.control_plane.get_workspace(workspace)
        with self.services.context.database.session() as session:
            repository = TaskRepository(session)
            tallies = repository.status_tallies(
                workspace_id=workspace_record.id,
                start=started_at,
                end=ended_at,
                app_id=app_id,
            )
            # Timings come from the rows that have both ends, which is a smaller
            # set than the tally counts and the only one percentiles are defined
            # over. Counting from these instead would drop every task still
            # running from the total.
            durations = repository.duration_samples(
                workspace_id=workspace_record.id,
                app_id=app_id,
                start=started_at,
                end=ended_at,
            )
        status_counts = Counter[TaskStatus]()
        for tally in tallies:
            status_counts[tally.status] += tally.count
        total = sum(status_counts.values())
        runtimes = sorted(_sample_runtime_ms(sample) for sample in durations)
        startups = sorted(_sample_startup_ms(sample) for sample in durations)
        failed = status_counts[TaskStatus.Failed]
        return TaskMetricsSummary(
            total=total,
            status_counts=dict(status_counts),
            completed=status_counts[TaskStatus.Complete],
            failed=failed,
            cancelled=status_counts[TaskStatus.Cancelled],
            failure_rate=failed / total if total else 0.0,
            average_runtime_ms=sum(runtimes) / len(runtimes) if runtimes else None,
            runtime_ms_p50=_percentile(runtimes, 0.50),
            runtime_ms_p95=_percentile(runtimes, 0.95),
            runtime_ms_p99=_percentile(runtimes, 0.99),
            startup_ms_p50=_percentile(startups, 0.50),
            startup_ms_p95=_percentile(startups, 0.95),
        )

    def iter_containers(
        self,
        workspace: str,
        *,
        status: ContainerStatus | None = None,
        page_size: int = 100,
    ) -> Iterator[ContainerRecord]:
        workspace_record = self.control_plane.get_workspace(workspace)
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            page = self.services.containers.page(
                workspace_id=workspace_record.id,
                statuses=(status,) if status is not None else (),
                cursor=cursor,
                limit=page_size,
            )
            yield from page.data
            if not page.next or page.next in seen_cursors:
                return
            seen_cursors.add(page.next)
            cursor = page.next

    def list_containers_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> tuple[ContainerRecord, ...]:
        with self.services.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            return tuple(ContainerRepository(session).list(workspace_id=workspace_id))

    def capture_active_container_shutdown_targets_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> list[ContainerShutdownTarget]:
        with self.services.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            return ContainerRepository(session).list_active_shutdown_targets(
                workspace_id=workspace_id
            )

    def container_page(
        self,
        workspace: str,
        *,
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
        statuses: tuple[ContainerStatus, ...] = (),
        limit: int = 100,
        cursor: str | None = None,
    ) -> CursorPage[ContainerStateWithApp]:
        workspace_record = self.control_plane.get_workspace(workspace)
        page = self.services.containers.page(
            workspace_id=workspace_record.id,
            app_id=app_id,
            stub_ids=stub_ids,
            statuses=statuses,
            cursor=cursor,
            limit=limit,
        )
        # One lookup for the page's stubs, not one per row: a hundred containers
        # used to mean a hundred sessions against a connection budget the whole
        # deployment shares, which is how a dashboard left open exhausted it.
        app_ids = self.control_plane.stub_app_ids(
            [
                container.stub_id
                for container in page.data
                if not container.app_id and container.stub_id
            ],
            workspace_id=workspace_record.id,
        )
        return CursorPage(
            data=tuple(
                ContainerStateWithApp(
                    container=container,
                    app_id=container.app_id or app_ids.get(container.stub_id or "", ""),
                )
                for container in page.data
            ),
            next=page.next,
        )

    def get_container(
        self,
        container_id: str,
        *,
        workspace: str | None = None,
    ) -> ContainerRecord:
        container = self.services.containers.get(container_id)
        if workspace is not None:
            workspace_record = self.control_plane.get_workspace(workspace)
            if container.workspace_id != workspace_record.id:
                msg = f"container not found in workspace: {container_id}"
                raise NotFoundError(msg)
        return container

    def container_view(
        self,
        container_id: str,
        *,
        workspace: str,
        can_write: bool,
    ) -> ContainerView:
        container = self.get_container(container_id, workspace=workspace)
        workload = self._container_workload(container, workspace=workspace)
        app = self._container_app(container, workload=workload, workspace=workspace)
        deployment = self._container_deployment(workload, workspace=workspace)
        run = self._container_run(container, workspace=workspace)
        running = container.status is ContainerStatus.Running
        active = container.status in {ContainerStatus.Pending, ContainerStatus.Running}
        sandbox = workload is not None and workload.kind is StubKind.Sandbox
        return ContainerView.model_validate(
            {
                **container.model_dump(),
                "app": app,
                "workload": workload,
                "deployment": deployment,
                "run_name": run.name if run is not None else None,
                "run_status": run.status if run is not None else None,
                "expires_at": _container_expires_at(container, workload),
                "actions": ContainerActionCapabilities(
                    can_stop=can_write and active,
                    can_shell=can_write and running,
                    can_create_image=can_write and running and sandbox,
                    can_snapshot_memory=can_write and running and sandbox,
                ),
            }
        )

    def stop_container(
        self,
        workspace: str,
        container_id: str,
    ) -> ContainerRecord:
        self.get_container(container_id, workspace=workspace)
        # A person asked for this one, so it says so. Left unstated the stop
        # would settle the same way and tell its owner nothing.
        return self.services.containers.stop(container_id, reason=StopContainerReason.User)

    def delete_container(self, workspace: str, container_id: str) -> None:
        self.get_container(container_id, workspace=workspace)
        self.services.containers.delete(container_id)

    def stop_all_containers(self, workspace: str) -> tuple[ContainerRecord, ...]:
        stopped: list[ContainerRecord] = []
        for container_status in (ContainerStatus.Running, ContainerStatus.Pending):
            for container in self.iter_containers(workspace, status=container_status):
                stopped.append(self.stop_container(workspace, container.id))
        return tuple(stopped)

    def stop_all_containers_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> tuple[ContainerRecord, ...]:
        """Stop only containers captured as active for a fenced Deleting workspace."""
        with self.services.context.database.session() as session:
            active = ContainerRepository(session).list(
                workspace_id=workspace_id,
                statuses=(ContainerStatus.Running.value, ContainerStatus.Pending.value),
            )
        return tuple(
            self.services.containers.stop_for_workspace_deletion(
                container.id,
                workspace_id=workspace_id,
            )
            for container in active
        )

    def _container_workload(
        self,
        container: ContainerRecord,
        *,
        workspace: str,
    ) -> StubRecord | None:
        if container.stub_id is None:
            return None
        try:
            return self.control_plane.get_stub(container.stub_id, workspace=workspace)
        except NotFoundError:
            return None

    def _container_app(
        self,
        container: ContainerRecord,
        *,
        workload: StubRecord | None,
        workspace: str,
    ) -> AppRecord | None:
        app_id = container.app_id or (workload.app_id if workload is not None else None)
        if app_id is None:
            return None
        try:
            return self.services.apps.get(app_id, workspace=workspace)
        except NotFoundError:
            return None

    def _container_deployment(
        self,
        workload: StubRecord | None,
        *,
        workspace: str,
    ) -> Deployment | None:
        if workload is None or workload.deployment_id is None:
            return None
        return next(
            (
                deployment
                for deployment in self.services.deployments.list(workspace=workspace)
                if deployment.id == workload.deployment_id
            ),
            None,
        )

    def _container_run(
        self,
        container: ContainerRecord,
        *,
        workspace: str,
    ) -> Task | None:
        if container.task_id is None:
            return None
        workspace_record = self.control_plane.get_workspace(workspace)
        try:
            run = self.services.tasks.get(container.task_id)
        except NotFoundError:
            return None
        return run if run.workspace_id == workspace_record.id else None

    def logs(
        self,
        workspace: str,
        *,
        object_id: str | None = None,
        object_type: str | None = None,
        stub_id: str | None = None,
        app_id: str | None = None,
        task_id: str | None = None,
        container_id: str | None = None,
        machine_id: str | None = None,
        worker_id: str | None = None,
        query: str | None = None,
        limit: int = 100,
        page: int = 0,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        seq_num: int | None = None,
        wait_seconds: float | None = None,
        clamp: bool | None = None,
    ) -> LogQueryResponse:
        self.control_plane.get_workspace(workspace)
        log_query = LogStreamQuery(
            workspace_id=workspace,
            object_id=object_id or "",
            object_type=object_type or "",
            stub_id=stub_id or "",
            app_id=app_id or "",
            task_id=task_id or "",
            container_id=container_id or "",
            machine_id=machine_id or "",
            worker_id=worker_id or "",
            query=query or "",
            limit=limit,
            page=page,
            start_time=start_time,
            end_time=end_time,
            seq_num=seq_num,
            wait_seconds=wait_seconds,
            clamp=clamp,
        )
        with self.services.context.database.session() as session:
            entries = LogRepository(session).list_across_workspaces(log_query)
        offset = log_query.page * log_query.limit
        data = entries[offset : offset + log_query.limit]
        next_page = str(log_query.page + 1) if offset + log_query.limit < len(entries) else ""
        return LogQueryResponse(
            object_id=log_query.object_id,
            object_type=_log_object_type(log_query.object_type),
            data=tuple(LogRecord.from_entry(item, workspace_id=workspace) for item in data),
            next=next_page,
            count=len(entries),
            total_expected=len(entries),
        )

    def event_history(
        self,
        workspace: str,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        task_id: str | None = None,
        container_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> EventQueryResponse:
        workspace_record = self.control_plane.get_workspace(workspace)
        if task_id:
            resource_type = TASK_EVENT_RESOURCE_TYPE
            resource_id = task_id
            container_id = None
        elif container_id:
            resource_type = None
            resource_id = None
        offset = _parse_cursor(cursor)
        # Workspace rows only. A cluster-scoped event carries no workspace
        # because it belongs to the platform rather than to a customer, so
        # folding those in hands every workspace every other workspace's.
        data = self.services.events.list(
            workspace_id=workspace_record.id,
            resource_type=resource_type,
            resource_id=resource_id,
            container_id=container_id,
            limit=limit,
            offset=offset,
        )
        total = self.services.events.count(
            workspace_id=workspace_record.id,
            resource_type=resource_type,
            resource_id=resource_id,
            container_id=container_id,
        )
        next_cursor = str(offset + limit) if offset + limit < total else ""
        return EventQueryResponse(data=tuple(data), next=next_cursor, count=total)
