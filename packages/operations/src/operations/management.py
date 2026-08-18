from __future__ import annotations

import base64
import binascii
from collections import Counter
from collections.abc import Iterator
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
from database.repositories.apps import AppSummaryRepository, DeploymentRepository
from database.repositories.execution import (
    LogRepository,
    RelatedTaskRecord,
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
from shared.container_requests import ContainerShutdownTarget, WorkerStartupKind
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.deployment_records import Deployment
from shared.deployments import DeploymentKind
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.client_manifests import INVOKABLE_DEPLOYMENT_KINDS, ClientManifestResource
from shared.http.observability import (
    EventQueryResponse,
    LogObjectType,
    LogQueryResponse,
    LogRecord,
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
    activity_24h: tuple[int, ...] = ()
    failures_24h: tuple[int, ...] = ()
    last_deployed_at: datetime | None = None


class TaskActionCapabilities(ContractModel):
    can_cancel: bool = False
    can_rerun: bool = False
    can_shell: bool = False


class TaskView(Task):
    app: AppRecord | None = None
    workload: StubRecord | None = None
    deployment: Deployment | None = None
    container: ContainerRecord | None = None
    actions: TaskActionCapabilities = Field(default_factory=TaskActionCapabilities)


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


def _task_view(record: RelatedTaskRecord, *, can_write: bool) -> TaskView:
    terminal = is_terminal_task_status(record.task.status)
    rerunnable_stub = record.workload is not None and record.workload.kind is StubKind.Function
    actions = TaskActionCapabilities(
        can_cancel=can_write and not terminal,
        can_rerun=can_write and terminal and rerunnable_stub,
        can_shell=(
            can_write
            and record.container is not None
            and record.container.status is ContainerStatus.Running
            and record.task.stub_id is not None
        ),
    )
    return TaskView.model_validate(
        {
            **record.task.model_dump(),
            "app": record.app,
            "workload": record.workload,
            "deployment": record.deployment,
            "container": record.container,
            "actions": actions,
        }
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


_INTERVAL_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _interval_seconds(interval: str) -> int:
    normalized = interval.strip().lower()
    unit = normalized[-1:] if normalized else ""
    amount = normalized[:-1]
    if unit not in _INTERVAL_UNIT_SECONDS or not amount.isdigit() or int(amount) < 1:
        msg = f"invalid metrics interval {interval!r}; expected forms like 30s, 1m, 5m, 1h, 1d"
        raise InvalidInputError(msg)
    return int(amount) * _INTERVAL_UNIT_SECONDS[unit]


@dataclass
class ManagementService:
    services: ManagementServices

    @property
    def control_plane(self) -> ControlPlaneService:
        return ControlPlaneService(
            self.services.context,
            workspace_changes=self.services.deployments.workspace_changes,
        )

    def deployment_ids_for_workspace(self, workspace: str) -> set[str]:
        return {
            resource.deployment.id
            for resource in self.services.deployment_resources.list(
                workspace=workspace, active=None
            )
        }

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
                    activity_24h=(tuple(facts.activity_24h) if facts is not None else (0,) * 24),
                    failures_24h=(tuple(facts.failures_24h) if facts is not None else (0,) * 24),
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
    ) -> TaskView:
        workspace_record = self.control_plane.get_workspace(workspace)
        with self.services.context.database.session() as session:
            related = TaskRepository(session).get_with_related(
                task_id,
                workspace_id=workspace_record.id,
            )
        if related is None:
            msg = f"task not found in workspace: {task_id}"
            raise NotFoundError(msg)
        return _task_view(related, can_write=can_write)

    def workspace_task(self, workspace: str, task_id: str) -> Task:
        return self.task_detail(workspace, task_id)

    def task_counts_by_deployment(self, workspace: str) -> tuple[TaskCountByDeployment, ...]:
        counts: dict[str, Counter[TaskStatus]] = {}
        for task in self._tasks_for_workspace(workspace):
            deployment_id = task.deployment_id or ""
            counts.setdefault(deployment_id, Counter())[task.status] += 1
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
        app_id: str | None = None,
        stub_id: str | None = None,
    ) -> tuple[TaskTimeWindowBucket, ...]:
        if window_seconds <= 0:
            msg = "window_seconds must be greater than zero"
            raise InvalidInputError(msg)
        buckets: dict[datetime, Counter[TaskStatus]] = {}
        for task in self._tasks_for_workspace(workspace):
            if app_id is not None and task.app_id != app_id:
                continue
            if stub_id is not None and task.stub_id != stub_id:
                continue
            bucket = _bucket_start(task.created_at, window_seconds)
            buckets.setdefault(bucket, Counter())[task.status] += 1
        return tuple(
            TaskTimeWindowBucket(
                timestamp=timestamp,
                count=sum(counter.values()),
                status_counts=dict(counter),
            )
            for timestamp, counter in sorted(buckets.items())
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
        workspace_task_ids = {task.id for task in self._tasks_for_workspace(workspace)}
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

        Writing `cancelled` on the row is all a cancel used to be, and for a
        function that left the handler running to completion: the caller was
        told their work had stopped while it went on producing side effects and
        being billed. Nothing inside the container watches the row, so reaching
        the work means going through the service that knows what stopping this
        workload does to the invocations beside it.

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
        tasks = [
            task
            for task in self._tasks_for_workspace(workspace)
            if started_at <= task.created_at <= ended_at
            and (app_id is None or task.app_id == app_id)
        ]
        status_counts = Counter(task.status for task in tasks)
        runtimes = sorted(value for task in tasks if (value := _runtime_ms(task)) is not None)
        startups = sorted(value for task in tasks if (value := _startup_ms(task)) is not None)
        failed = status_counts[TaskStatus.Failed]
        return TaskMetricsSummary(
            total=len(tasks),
            status_counts=dict(status_counts),
            completed=status_counts[TaskStatus.Complete],
            failed=failed,
            cancelled=status_counts[TaskStatus.Cancelled],
            failure_rate=failed / len(tasks) if tasks else 0.0,
            average_runtime_ms=sum(runtimes) / len(runtimes) if runtimes else None,
            runtime_ms_p50=_percentile(runtimes, 0.50),
            runtime_ms_p95=_percentile(runtimes, 0.95),
            runtime_ms_p99=_percentile(runtimes, 0.99),
            startup_ms_p50=_percentile(startups, 0.50),
            startup_ms_p95=_percentile(startups, 0.95),
        )

    def _tasks_for_workspace(self, workspace: str) -> list[Task]:
        workspace_record = self.control_plane.get_workspace(workspace)
        return self.services.tasks.list(workspace_id=workspace_record.id)

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
        return CursorPage(
            data=tuple(
                ContainerStateWithApp(
                    container=container,
                    app_id=container.app_id or self._app_id_for_stub(container.stub_id),
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
        return self.services.containers.stop(container_id)

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
        data = self.services.events.list(
            workspace_id=workspace_record.id,
            include_cluster=True,
            resource_type=resource_type,
            resource_id=resource_id,
            container_id=container_id,
            limit=limit,
            offset=offset,
        )
        total = self.services.events.count(
            workspace_id=workspace_record.id,
            include_cluster=True,
            resource_type=resource_type,
            resource_id=resource_id,
            container_id=container_id,
        )
        next_cursor = str(offset + limit) if offset + limit < total else ""
        return EventQueryResponse(data=tuple(data), next=next_cursor, count=total)

    def _app_id_for_stub(self, stub_id: str | None) -> str:
        if not stub_id:
            return ""
        try:
            stub = self.control_plane.get_stub(stub_id)
        except NotFoundError:
            return ""
        return stub.app_id or ""
