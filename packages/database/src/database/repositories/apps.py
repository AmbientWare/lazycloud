from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from database.mappers.apps import (
    app_container_shutdown_intent_from_table,
    app_deployment_intent_from_table,
    app_record_from_table,
    write_app_row,
)
from database.records.apps import (
    AppContainerShutdownIntentRecord,
    AppDeploymentIntentRecord,
    AppRecord,
    AutoscalingStubConfig,
    AutoscalingStubRecord,
    AutoscalingStubRuntimeConfig,
    StubRecord,
)
from database.repositories.common import (
    TableRepositoryConfig,
    WorkspaceTableRepository,
    bucket_index,
    names_by_id,
)
from database.repositories.identity import WorkspaceRepository
from database.tables.apps import (
    AppContainerShutdownIntentTable,
    AppDeploymentIntentTable,
    AppTable,
    CronJobTable,
    DeploymentTable,
    StubTable,
)
from database.tables.execution import TaskTable
from database.tables.images import CheckpointTable
from database.tables.orchestration import ContainerTable
from pydantic import BaseModel, JsonValue
from shared.app_lifecycle import (
    UNFINISHED_APP_LIFECYCLE_STATES,
    AppDeploymentIntentTarget,
    AppLifecycleState,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.cron import CronJobRecord
from shared.deployment_records import Deployment
from shared.deployments import DeploymentKind, StubKind
from shared.enums import StringEnum
from shared.errors import ConflictError
from shared.identity import WorkspaceStatus
from shared.tasks import TaskStatus
from shared.workload_config import StubAutoscalerConfig, StubTaskPolicy
from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql.elements import ColumnElement


class DeploymentResourceRow(BaseModel):
    app: AppRecord
    deployment_payload: dict[str, JsonValue]
    deployment_app_id: str | None = None
    deployment_stub_id: str | None = None
    stub_payload: dict[str, JsonValue]


class AppRunningResult(BaseModel):
    app_id: str
    running: int


class AppTaskBucketResult(BaseModel):
    app_id: str
    bucket: int
    runs: int
    failed: int | None
    pending: int | None
    succeeded: int | None


class ActivityStartSource(StringEnum):
    """Which rows a start is counted from.

    Owned here rather than taken from the HTTP measure a reader picked: only two
    of those measures are starts at all, and a query that branched on the wider
    enum would have to pick a table for a measure that names no table.
    """

    Containers = "containers"
    Tasks = "tasks"


class AppActivityCountResult(BaseModel):
    """One app's starts inside one interval of an activity window.

    Named by workspace and app together. An account reads several workspaces at
    once and two of them may hold apps of the same name, and unattributed starts
    carry no app id at all — the same absent key in every workspace.
    """

    workspace_id: str
    app_id: str | None = None
    index: int
    count: int


class AppExecutionSummary(BaseModel):
    app_id: str
    running_containers: int = 0
    runs_24h: int = 0
    failed_runs_24h: int = 0
    pending_runs_24h: int = 0
    succeeded_runs_24h: int = 0
    activity_24h: list[int]
    failures_24h: list[int]
    pending_24h: list[int]
    succeeded_24h: list[int]


@dataclass(slots=True)
class AppRepository:
    session: Session

    def upsert(self, app: AppRecord) -> AppRecord:
        row = self.session.get(AppTable, app.id)
        if row is None:
            row = AppTable(id=app.id)
            self.session.add(row)
        elif str(row.workspace_id) != app.workspace_id:
            raise ValueError("app ownership cannot change")
        write_app_row(row, app)
        self.session.flush()
        return app_record_from_table(row)

    def get(
        self,
        app_id: str,
        *,
        workspace_id: str,
        include_deleted: bool = False,
    ) -> AppRecord | None:
        statement = select(AppTable).where(
            AppTable.id == app_id,
            AppTable.workspace_id == workspace_id,
        )
        if not include_deleted:
            statement = statement.where(AppTable.deleted_at.is_(None))
        row = self.session.scalars(statement).first()
        return app_record_from_table(row) if row is not None else None

    def names(self, app_ids: Sequence[str]) -> dict[str, str]:
        """What each of these apps is called, deleted ones included.

        A deleted app keeps its name here because the work it did still happened
        and still has to be labelled.
        """

        return names_by_id(self.session, AppTable.id, AppTable.name, app_ids)

    def active_by_ids(self, app_ids: Sequence[str]) -> dict[str, bool]:
        wanted = tuple(dict.fromkeys(app_ids))
        active = {app_id: False for app_id in wanted}
        if not wanted:
            return active
        rows = self.session.scalars(
            select(AppTable.id).where(
                AppTable.id.in_(wanted),
                AppTable.lifecycle_state == AppLifecycleState.Active.value,
                AppTable.deleted_at.is_(None),
            )
        )
        for app_id in rows:
            active[str(app_id)] = True
        return active

    def get_across_workspaces(
        self,
        app_id: str,
        *,
        include_deleted: bool = False,
    ) -> AppRecord | None:
        """System lookup for reconcilers/workers acting under their own authority."""
        statement = select(AppTable).where(AppTable.id == app_id)
        if not include_deleted:
            statement = statement.where(AppTable.deleted_at.is_(None))
        row = self.session.scalars(statement).first()
        return app_record_from_table(row) if row is not None else None

    def get_by_name(
        self,
        name: str,
        *,
        workspace_id: str,
        include_deleted: bool = False,
        for_update: bool = False,
    ) -> AppRecord | None:
        statement = select(AppTable).where(
            AppTable.workspace_id == workspace_id,
            AppTable.name == name,
        )
        if not include_deleted:
            statement = statement.where(AppTable.deleted_at.is_(None))
        statement = statement.order_by(
            AppTable.version.desc(), AppTable.updated_at.desc(), AppTable.id.desc()
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return app_record_from_table(row) if row is not None else None

    def get_for_update(
        self,
        app_id: str,
        *,
        workspace_id: str | None = None,
        include_deleted: bool = False,
    ) -> AppRecord | None:
        statement = select(AppTable).where(AppTable.id == app_id)
        if workspace_id is not None:
            statement = statement.where(AppTable.workspace_id == workspace_id)
        if not include_deleted:
            statement = statement.where(AppTable.deleted_at.is_(None))
        row = self.session.scalars(statement.with_for_update()).first()
        return app_record_from_table(row) if row is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        include_deleted: bool = False,
    ) -> list[AppRecord]:
        statement = select(AppTable).where(AppTable.workspace_id == workspace_id)
        if not include_deleted:
            statement = statement.where(AppTable.deleted_at.is_(None))
        statement = statement.order_by(AppTable.name, AppTable.version, AppTable.id)
        return [app_record_from_table(row) for row in self.session.scalars(statement)]

    def list_across_workspaces(self, *, include_deleted: bool = False) -> list[AppRecord]:
        statement = select(AppTable)
        if not include_deleted:
            statement = statement.where(AppTable.deleted_at.is_(None))
        statement = statement.order_by(
            AppTable.workspace_id, AppTable.name, AppTable.version, AppTable.id
        )
        return [app_record_from_table(row) for row in self.session.scalars(statement)]

    def claim_unfinished(
        self,
        *,
        claim_id: str,
        stale_before: datetime,
        limit: int,
    ) -> list[AppRecord]:
        states = tuple(state.value for state in UNFINISHED_APP_LIFECYCLE_STATES)
        statement = (
            select(AppTable)
            .where(
                AppTable.lifecycle_state.in_(states),
                or_(
                    AppTable.reconcile_claim_id.is_(None),
                    AppTable.reconcile_claimed_at.is_(None),
                    AppTable.reconcile_claimed_at < stale_before,
                ),
            )
            .order_by(AppTable.updated_at, AppTable.id)
            .limit(max(limit, 0))
            .with_for_update(skip_locked=True)
        )
        rows = list(self.session.scalars(statement))
        claimed_at = datetime.now(UTC)
        for row in rows:
            row.reconcile_claim_id = claim_id
            row.reconcile_claimed_at = claimed_at
        self.session.flush()
        return [app_record_from_table(row) for row in rows]


@dataclass(slots=True)
class AppDeploymentIntentRepository:
    session: Session

    def replace(
        self,
        *,
        app_id: str,
        deployment_ids: list[str],
        operation_revision: int,
        target: AppDeploymentIntentTarget,
    ) -> list[AppDeploymentIntentRecord]:
        self.clear(app_id=app_id)
        rows = [
            AppDeploymentIntentTable(
                app_id=app_id,
                deployment_id=deployment_id,
                operation_revision=operation_revision,
                target=target.value,
            )
            for deployment_id in sorted(set(deployment_ids))
        ]
        self.session.add_all(rows)
        self.session.flush()
        return [app_deployment_intent_from_table(row) for row in rows]

    def retarget(
        self,
        *,
        app_id: str,
        operation_revision: int,
        target: AppDeploymentIntentTarget,
    ) -> list[AppDeploymentIntentRecord]:
        rows = list(
            self.session.scalars(
                select(AppDeploymentIntentTable)
                .where(AppDeploymentIntentTable.app_id == app_id)
                .order_by(AppDeploymentIntentTable.deployment_id)
                .with_for_update()
            )
        )
        for row in rows:
            row.operation_revision = operation_revision
            row.target = target.value
        self.session.flush()
        return [app_deployment_intent_from_table(row) for row in rows]

    def list(self, *, app_id: str) -> list[AppDeploymentIntentRecord]:
        rows = self.session.scalars(
            select(AppDeploymentIntentTable)
            .where(AppDeploymentIntentTable.app_id == app_id)
            .order_by(AppDeploymentIntentTable.deployment_id)
        )
        return [app_deployment_intent_from_table(row) for row in rows]

    def get_for_update(
        self,
        *,
        app_id: str,
        deployment_id: str,
    ) -> AppDeploymentIntentRecord | None:
        row = self.session.scalars(
            select(AppDeploymentIntentTable)
            .where(
                AppDeploymentIntentTable.app_id == app_id,
                AppDeploymentIntentTable.deployment_id == deployment_id,
            )
            .with_for_update()
        ).first()
        return app_deployment_intent_from_table(row) if row is not None else None

    def update_publication(
        self,
        intent: AppDeploymentIntentRecord,
    ) -> AppDeploymentIntentRecord:
        row = self.session.get(
            AppDeploymentIntentTable,
            (intent.app_id, intent.deployment_id),
        )
        if row is None:
            raise KeyError((intent.app_id, intent.deployment_id))
        row.operation_revision = intent.operation_revision
        row.target = intent.target.value
        row.event_id = intent.event_id
        row.event_created_at = intent.event_created_at
        row.workspace_change_published_at = intent.workspace_change_published_at
        row.updated_at = intent.updated_at
        self.session.flush()
        return app_deployment_intent_from_table(row)

    def clear(self, *, app_id: str) -> None:
        self.session.execute(
            delete(AppDeploymentIntentTable).where(AppDeploymentIntentTable.app_id == app_id)
        )


@dataclass(slots=True)
class AppContainerShutdownIntentRepository:
    session: Session

    def capture_active(
        self,
        *,
        app_id: str,
        workspace_id: str,
        operation_revision: int,
    ) -> list[AppContainerShutdownIntentRecord]:
        rows = list(
            self.session.scalars(
                select(AppContainerShutdownIntentTable)
                .where(AppContainerShutdownIntentTable.app_id == app_id)
                .with_for_update()
            )
        )
        known = {str(row.container_id): row for row in rows}
        containers = [
            ContainerRecord.model_validate(row.payload)
            for row in self.session.scalars(
                select(ContainerTable).where(
                    ContainerTable.workspace_id == workspace_id,
                    ContainerTable.app_id == app_id,
                    ContainerTable.status.in_(
                        (ContainerStatus.Pending.value, ContainerStatus.Running.value)
                    ),
                )
            )
        ]
        for container in containers:
            worker_id = container.runtime_worker_id or container.worker_id or ""
            existing = known.get(container.id)
            if existing is not None:
                if not existing.worker_id and worker_id:
                    existing.worker_id = worker_id
                continue
            row = AppContainerShutdownIntentTable(
                app_id=app_id,
                container_id=container.id,
                worker_id=worker_id,
                operation_revision=operation_revision,
            )
            self.session.add(row)
            rows.append(row)
        self.session.flush()
        return [app_container_shutdown_intent_from_table(row) for row in rows]

    def list(self, *, app_id: str) -> list[AppContainerShutdownIntentRecord]:
        rows = self.session.scalars(
            select(AppContainerShutdownIntentTable)
            .where(AppContainerShutdownIntentTable.app_id == app_id)
            .order_by(AppContainerShutdownIntentTable.container_id)
        )
        return [app_container_shutdown_intent_from_table(row) for row in rows]

    def clear(self, *, app_id: str) -> None:
        self.session.execute(
            delete(AppContainerShutdownIntentTable).where(
                AppContainerShutdownIntentTable.app_id == app_id
            )
        )


@dataclass(slots=True)
class AppSummaryRepository:
    session: Session

    def execution_summaries(
        self,
        *,
        workspace_id: str,
        start: datetime,
        bucket_seconds: int = 3600,
        bucket_count: int = 24,
    ) -> dict[str, AppExecutionSummary]:
        """Aggregate all app run and live-container facts with constant query count."""
        summaries: dict[str, AppExecutionSummary] = {}

        running_statement = (
            select(
                ContainerTable.app_id.label("app_id"),
                func.count(ContainerTable.id).label("running"),
            )
            .where(
                ContainerTable.workspace_id == workspace_id,
                ContainerTable.app_id.is_not(None),
                ContainerTable.status == ContainerStatus.Running.value,
            )
            .group_by(ContainerTable.app_id)
        )
        for raw in self.session.execute(running_statement).mappings():
            row = AppRunningResult.model_validate(raw)
            summary = _app_execution_summary(row.app_id, bucket_count)
            summary.running_containers = row.running
            summaries[summary.app_id] = summary

        bucket = bucket_index(
            TaskTable.created_at,
            start=start,
            width_seconds=bucket_seconds,
        ).label("bucket")
        failed = func.sum(
            case(
                (
                    TaskTable.status.in_(
                        [
                            TaskStatus.Failed.value,
                            TaskStatus.Timeout.value,
                            TaskStatus.Expired.value,
                        ]
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("failed")
        pending = func.sum(
            case(
                (
                    TaskTable.status.in_(
                        [
                            TaskStatus.Pending.value,
                            TaskStatus.Running.value,
                            TaskStatus.Retry.value,
                        ]
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("pending")
        # Counted rather than derived as runs less failed and pending, because
        # that difference is not success: a cancelled task is neither, and
        # deriving it would paint the one colour a reader trusts most over work
        # nobody finished. `complete` is the only status that succeeded, and it
        # is the same one the app view resolves its green band from.
        succeeded = func.sum(
            case((TaskTable.status == TaskStatus.Complete.value, 1), else_=0)
        ).label("succeeded")
        task_statement = (
            select(
                TaskTable.app_id,
                bucket,
                func.count(TaskTable.id).label("runs"),
                failed,
                pending,
                succeeded,
            )
            .where(
                TaskTable.workspace_id == workspace_id,
                TaskTable.app_id.is_not(None),
                TaskTable.created_at >= start,
            )
            .group_by(TaskTable.app_id, bucket)
            .order_by(TaskTable.app_id, bucket)
        )
        for raw in self.session.execute(task_statement).mappings():
            row = AppTaskBucketResult.model_validate(raw)
            key = row.app_id
            summary = summaries.setdefault(key, _app_execution_summary(key, bucket_count))
            index = row.bucket
            if 0 <= index < bucket_count:
                run_count = row.runs
                failure_count = row.failed or 0
                pending_count = row.pending or 0
                succeeded_count = row.succeeded or 0
                summary.activity_24h[index] = run_count
                summary.failures_24h[index] = failure_count
                summary.pending_24h[index] = pending_count
                summary.succeeded_24h[index] = succeeded_count
                summary.runs_24h += run_count
                summary.failed_runs_24h += failure_count
                summary.pending_runs_24h += pending_count
                summary.succeeded_runs_24h += succeeded_count
        return summaries

    def activity_by_app(
        self,
        *,
        workspace_ids: Sequence[str],
        source: ActivityStartSource,
        start: datetime,
        end: datetime,
        window_seconds: int,
    ) -> tuple[AppActivityCountResult, ...]:
        """How many things these workspaces started per app, per interval.

        Both sources are counted by `created_at` — the instant the work was asked
        for — so a long-running container is one start in the interval it began,
        not a smear across every interval it survived.

        Rows come back sparse and unordered beyond the grouping, carrying ids and
        no names; densifying the window, resolving what an id is called and
        deciding which apps a reader sees belong to the caller, not to the query.
        """

        if not workspace_ids:
            return ()
        table: type[ContainerTable] | type[TaskTable] = (
            ContainerTable if source is ActivityStartSource.Containers else TaskTable
        )
        index = bucket_index(table.created_at, start=start, width_seconds=window_seconds).label(
            "index"
        )
        rows = self.session.execute(
            select(
                table.workspace_id.label("workspace_id"),
                table.app_id.label("app_id"),
                index,
                func.count(table.id).label("count"),
            )
            .where(
                table.workspace_id.in_(workspace_ids),
                table.created_at >= start,
                table.created_at < end,
            )
            .group_by(table.workspace_id, table.app_id, index)
        ).mappings()
        return tuple(AppActivityCountResult.model_validate(row) for row in rows)


def _app_execution_summary(app_id: str, bucket_count: int) -> AppExecutionSummary:
    return AppExecutionSummary(
        app_id=app_id,
        activity_24h=[0] * bucket_count,
        failures_24h=[0] * bucket_count,
        pending_24h=[0] * bucket_count,
        succeeded_24h=[0] * bucket_count,
    )


@dataclass(slots=True)
class StubRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[StubRecord]:
        return WorkspaceTableRepository(self.session, TableRepositoryConfig(StubTable, StubRecord))

    def upsert(self, stub: StubRecord) -> StubRecord:
        return self.records.upsert(stub, workspace_id=stub.workspace_id, name=stub.name)

    def get(self, stub_id: str, *, workspace_id: str) -> StubRecord | None:
        return self.records.get(stub_id, workspace_id=workspace_id)

    def get_across_workspaces(self, stub_id: str) -> StubRecord | None:
        """System lookup for scheduler/worker/runner paths resolving placed work."""
        return self.records.get_across_workspaces(stub_id)

    def list(self, *, workspace_id: str) -> list[StubRecord]:
        return self.records.list(workspace_id=workspace_id)

    def list_autoscaling_across_workspaces(self) -> list[AutoscalingStubRecord]:
        rows = self.session.execute(
            select(
                StubTable.id,
                StubTable.workspace_id,
                StubTable.type,
                StubTable.app_id,
                StubTable.payload["deployment_id"].as_string(),
                StubTable.payload["config"]["runtime"]["cpu"],
                StubTable.payload["config"]["runtime"]["cpu_millicores"],
                StubTable.payload["config"]["runtime"]["gpu"],
                StubTable.payload["config"]["runtime"]["gpu_count"],
                StubTable.payload["config"]["runtime"]["timeout_seconds"],
                StubTable.payload["config"]["runtime"]["keep_warm"],
                StubTable.payload["config"]["runtime"]["workspace_gpu_quota"],
                StubTable.payload["config"]["runtime"]["workspace_cpu_quota_millicores"],
                StubTable.payload["config"]["autoscaler"],
                StubTable.payload["config"]["task_policy"],
                StubTable.payload["config"]["metadata"]["autoscaling_enabled"].as_boolean(),
            ).order_by(StubTable.created_at.desc(), StubTable.id.asc())
        ).tuples()
        return [
            AutoscalingStubRecord(
                id=id_,
                workspace_id=workspace_id,
                kind=StubKind(type_),
                app_id=app_id,
                deployment_id=deployment_id,
                config=AutoscalingStubConfig(
                    runtime=AutoscalingStubRuntimeConfig.model_validate(
                        {
                            field: value
                            for field, value in (
                                ("cpu", cpu),
                                ("cpu_millicores", cpu_millicores),
                                ("gpu", gpu),
                                ("gpu_count", gpu_count),
                                ("timeout_seconds", timeout_seconds),
                                ("keep_warm", keep_warm),
                                ("workspace_gpu_quota", workspace_gpu_quota),
                                (
                                    "workspace_cpu_quota_millicores",
                                    workspace_cpu_quota_millicores,
                                ),
                            )
                            if value is not None
                        }
                    ),
                    autoscaler=StubAutoscalerConfig.model_validate(autoscaler or {}),
                    task_policy=StubTaskPolicy.model_validate(task_policy or {}),
                    metadata=(
                        {"autoscaling_enabled": autoscaling_enabled}
                        if autoscaling_enabled is not None
                        else {}
                    ),
                ),
            )
            for (
                id_,
                workspace_id,
                type_,
                app_id,
                deployment_id,
                cpu,
                cpu_millicores,
                gpu,
                gpu_count,
                timeout_seconds,
                keep_warm,
                workspace_gpu_quota,
                workspace_cpu_quota_millicores,
                autoscaler,
                task_policy,
                autoscaling_enabled,
            ) in rows
        ]

    def app_ids_by_id(
        self,
        stub_ids: Sequence[str],
        *,
        workspace_id: str,
    ) -> dict[str, str]:
        """Which app each of these stubs belongs to, in one query.

        Resolved for a whole page at once because the caller has a page: asking
        per row turned one list request into a hundred round trips against a
        connection budget shared with everything else the deployment does.

        Scoped, unlike the per-row lookup it replaces, which read across every
        workspace to answer a question about one.
        """

        wanted = [stub_id for stub_id in dict.fromkeys(stub_ids) if stub_id]
        if not wanted:
            return {}
        rows = self.session.execute(
            select(StubTable.id, StubTable.app_id).where(
                StubTable.workspace_id == workspace_id,
                StubTable.id.in_(wanted),
            )
        )
        return {str(stub_id): str(app_id) for stub_id, app_id in rows if app_id}

    def list_for_app(self, *, workspace_id: str, app_id: str) -> list[StubRecord]:
        statement = select(StubTable).where(
            StubTable.workspace_id == workspace_id,
            StubTable.app_id == app_id,
        )
        return [StubRecord.model_validate(row.payload) for row in self.session.scalars(statement)]

    def get_for_update(self, stub_id: str, *, workspace_id: str) -> StubRecord | None:
        row = self.session.scalars(
            select(StubTable)
            .where(StubTable.id == stub_id, StubTable.workspace_id == workspace_id)
            .with_for_update()
        ).first()
        return StubRecord.model_validate(row.payload) if row is not None else None

    def get_by_name_for_update(self, name: str, *, workspace_id: str) -> StubRecord | None:
        row = self.session.scalars(
            select(StubTable)
            .where(StubTable.name == name, StubTable.workspace_id == workspace_id)
            .order_by(StubTable.created_at.asc(), StubTable.id.asc())
            .with_for_update()
        ).first()
        return StubRecord.model_validate(row.payload) if row is not None else None

    def registration_is_bound(self, stub_id: str) -> bool:
        statements = (
            select(AppTable.id).where(AppTable.stub_id == stub_id).limit(1),
            select(DeploymentTable.id).where(DeploymentTable.stub_id == stub_id).limit(1),
            select(TaskTable.id).where(TaskTable.stub_id == stub_id).limit(1),
            select(ContainerTable.id).where(ContainerTable.stub_id == stub_id).limit(1),
            select(CheckpointTable.id).where(CheckpointTable.stub_id == stub_id).limit(1),
        )
        return any(self.session.scalar(statement) is not None for statement in statements)

    def delete(self, stub_id: str, *, workspace_id: str) -> bool:
        return self.records.delete(stub_id, workspace_id=workspace_id)


@dataclass(slots=True)
class DeploymentRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[Deployment]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(DeploymentTable, Deployment),
        )

    def upsert(self, deployment: Deployment, *, workspace_id: str) -> Deployment:
        return self.records.upsert(
            deployment,
            workspace_id=workspace_id,
            name=deployment.name,
            status="active" if deployment.active else "inactive",
        )

    def assert_subdomain_unclaimed(
        self,
        subdomain: str,
        *,
        workspace_id: str,
        app_id: str | None,
        name: str,
        kind: DeploymentKind,
    ) -> None:
        """Refuse a subdomain another resource already answers on.

        `uq_deployments_subdomain_version_active` only catches a digest collision when
        both resources reach the same version number. Two colliding resources sitting at
        different versions would otherwise share a hostname, and the edge would hand one
        tenant's traffic to the other's resource.
        """
        same_resource = and_(
            DeploymentTable.workspace_id == workspace_id,
            DeploymentTable.app_id.is_not_distinct_from(app_id),
            DeploymentTable.name == name,
            DeploymentTable.kind == kind.value,
        )
        conflict = self.session.execute(
            select(DeploymentTable.id)
            .where(DeploymentTable.subdomain == subdomain)
            .where(DeploymentTable.deleted_at.is_(None))
            .where(~same_resource)
            .limit(1)
        ).first()
        if conflict is not None:
            raise ConflictError(
                f"subdomain {subdomain} already belongs to another resource; "
                f"rename {name} to claim a different one"
            )

    def deactivate_for_workspace_deletion(
        self,
        deployment_id: str,
        *,
        workspace_id: str,
        now: datetime,
    ) -> Deployment | None:
        """System cleanup transition for an existing deployment in Deleting state."""
        workspace = WorkspaceRepository(self.session).lock_for_deletion(workspace_id)
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
        deployment = self.records.get(deployment_id, workspace_id=workspace_id)
        if deployment is None:
            return None
        deployment.active = False
        deployment.updated_at = now
        row = self.session.get(DeploymentTable, deployment_id)
        if row is None or str(row.workspace_id) != workspace_id:
            raise ConflictError(
                f"deployment disappeared during workspace deletion: {deployment_id}"
            )
        row.payload = deployment.model_dump(mode="json")
        row.active = False
        row.updated_at = now
        flag_modified(row, "payload")
        self.session.flush()
        return deployment

    def get(
        self,
        deployment_id: str,
        *,
        workspace_id: str,
        include_deleted: bool = False,
    ) -> Deployment | None:
        deployment = self.records.get(deployment_id, workspace_id=workspace_id)
        return _visible_deployment(deployment, include_deleted=include_deleted)

    def get_across_workspaces(
        self,
        deployment_id: str,
        *,
        include_deleted: bool = False,
    ) -> Deployment | None:
        """System lookup for scheduler/worker paths acting under their own authority."""
        deployment = self.records.get_across_workspaces(deployment_id)
        return _visible_deployment(deployment, include_deleted=include_deleted)

    def workspace_id(self, deployment_id: str) -> str | None:
        value = self.session.scalar(
            select(DeploymentTable.workspace_id).where(DeploymentTable.id == deployment_id)
        )
        return str(value) if value is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        app_id: str | None = None,
        active: bool | None = None,
        include_deleted: bool = False,
    ) -> list[Deployment]:
        return self._list(
            workspace_id=workspace_id,
            app_id=app_id,
            active=active,
            include_deleted=include_deleted,
        )

    def list_across_workspaces(
        self,
        *,
        app_id: str | None = None,
        active: bool | None = None,
        include_deleted: bool = False,
    ) -> list[Deployment]:
        """Operator/system listing over every workspace's deployments."""
        return self._list(
            workspace_id=None,
            app_id=app_id,
            active=active,
            include_deleted=include_deleted,
        )

    def active_by_ids(self, deployment_ids: Sequence[str]) -> dict[str, bool]:
        wanted = tuple(dict.fromkeys(deployment_ids))
        active = {deployment_id: False for deployment_id in wanted}
        if not wanted:
            return active
        rows = self.session.scalars(
            select(DeploymentTable.id).where(
                DeploymentTable.id.in_(wanted),
                DeploymentTable.active.is_(True),
                DeploymentTable.deleted_at.is_(None),
            )
        )
        for deployment_id in rows:
            active[str(deployment_id)] = True
        return active

    def _list(
        self,
        *,
        workspace_id: str | None,
        app_id: str | None,
        active: bool | None,
        include_deleted: bool,
    ) -> list[Deployment]:
        statement = select(DeploymentTable)
        if workspace_id is not None:
            statement = statement.where(DeploymentTable.workspace_id == workspace_id)
        if app_id is not None:
            statement = statement.where(DeploymentTable.app_id == app_id)
        if active is not None:
            statement = statement.where(DeploymentTable.active.is_(active))
        if not include_deleted:
            statement = statement.where(DeploymentTable.deleted_at.is_(None))
        statement = statement.order_by(
            DeploymentTable.created_at.desc(),
            DeploymentTable.id.asc(),
        )
        return [Deployment.model_validate(row.payload) for row in self.session.scalars(statement)]


def _visible_deployment(
    deployment: Deployment | None,
    *,
    include_deleted: bool,
) -> Deployment | None:
    if deployment is None:
        return None
    if deployment.deleted_at is not None and not include_deleted:
        return None
    return deployment


@dataclass(slots=True)
class DeploymentResourceRepository:
    session: Session

    def list(
        self,
        *,
        workspace_id: str | None,
        app: str | None,
        app_id: str | None,
        deployment_id: str | None,
        name: str | None,
        kinds: frozenset[DeploymentKind] | set[DeploymentKind] | None,
        version: int | None,
        active: bool | None,
    ) -> list[DeploymentResourceRow]:
        statement = (
            select(AppTable, DeploymentTable, StubTable)
            .join(DeploymentTable, DeploymentTable.app_id == AppTable.id)
            .join(StubTable, StubTable.id == DeploymentTable.stub_id)
            .where(AppTable.deleted_at.is_(None))
            .where(DeploymentTable.deleted_at.is_(None))
        )
        if workspace_id is not None:
            statement = statement.where(AppTable.workspace_id == workspace_id).where(
                DeploymentTable.workspace_id == workspace_id
            )
        if app is not None:
            statement = statement.where(AppTable.name == app)
        if app_id is not None:
            statement = statement.where(AppTable.id == app_id)
        if deployment_id is not None:
            statement = statement.where(DeploymentTable.id == deployment_id)
        if name is not None:
            statement = statement.where(DeploymentTable.name == name)
        if kinds is not None:
            statement = statement.where(
                DeploymentTable.kind.in_([deployment_kind.value for deployment_kind in kinds])
            )
        if version is not None:
            statement = statement.where(DeploymentTable.version == version)
        if active is not None:
            statement = statement.where(DeploymentTable.active.is_(active))
        statement = statement.order_by(
            DeploymentTable.kind.asc(),
            DeploymentTable.name.asc(),
            DeploymentTable.version.desc(),
            DeploymentTable.created_at.desc(),
        )
        return [
            DeploymentResourceRow(
                app=app_record_from_table(app_row),
                deployment_payload=deployment_row.payload,
                deployment_app_id=str(deployment_row.app_id) if deployment_row.app_id else None,
                deployment_stub_id=str(deployment_row.stub_id) if deployment_row.stub_id else None,
                stub_payload=stub_row.payload,
            )
            for app_row, deployment_row, stub_row in self.session.execute(statement).tuples()
        ]

    def hostnames_claimed_under(self, *, workspace_id: str) -> list[str]:
        """Every hostname a live deployment in this workspace currently answers on.

        Read before retiring a registration, so discarding a certificate cannot take
        a serving deployment offline as a side effect.
        """
        rows = self.session.execute(
            select(DeploymentTable.custom_hostname)
            .where(DeploymentTable.workspace_id == workspace_id)
            .where(DeploymentTable.deleted_at.is_(None))
            .where(DeploymentTable.active.is_(True))
            .where(DeploymentTable.custom_hostname.is_not(None))
            .distinct()
        ).scalars()
        return [hostname for hostname in rows if hostname]

    def get_by_custom_hostname(self, hostname: str) -> DeploymentResourceRow | None:
        """Resolve the resource that claimed a registered hostname, at its latest version.

        Not workspace-scoped, for the same reason `get_by_subdomain` is not: the edge
        has only the hostname. Safe because a deployment may claim a hostname only
        under a domain its own workspace registered, and the registration is unique
        across workspaces.
        """
        return self._resolve_host_row(DeploymentTable.custom_hostname == hostname)

    def get_by_subdomain(
        self,
        subdomain: str,
        *,
        version: int | None = None,
    ) -> DeploymentResourceRow | None:
        """Resolve the resource a public hostname addresses.

        `version=None` answers with the latest, which is what a bare hostname means.

        Deliberately not workspace-scoped, unlike every other lookup here: a request
        arriving at the edge carries no token, so the subdomain is the only routing key
        available and the row it finds is what establishes which workspace answers.
        That is safe only because a subdomain belongs to exactly one resource, which
        `assert_subdomain_unclaimed` establishes when the subdomain is minted.
        """
        match = DeploymentTable.subdomain == subdomain
        if version is not None:
            match = and_(match, DeploymentTable.version == version)
        return self._resolve_host_row(match)

    def _resolve_host_row(self, match: ColumnElement[bool]) -> DeploymentResourceRow | None:
        row = (
            self.session.execute(
                select(AppTable, DeploymentTable, StubTable)
                .join(DeploymentTable, DeploymentTable.app_id == AppTable.id)
                .join(StubTable, StubTable.id == DeploymentTable.stub_id)
                .where(AppTable.deleted_at.is_(None))
                .where(DeploymentTable.deleted_at.is_(None))
                .where(DeploymentTable.active.is_(True))
                .where(match)
                .order_by(DeploymentTable.version.desc())
                .limit(1)
            )
            .tuples()
            .first()
        )
        if row is None:
            return None
        app_row, deployment_row, stub_row = row
        return DeploymentResourceRow(
            app=app_record_from_table(app_row),
            deployment_payload=deployment_row.payload,
            deployment_app_id=str(deployment_row.app_id) if deployment_row.app_id else None,
            deployment_stub_id=str(deployment_row.stub_id) if deployment_row.stub_id else None,
            stub_payload=stub_row.payload,
        )


@dataclass(slots=True)
class CronJobRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[CronJobRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(CronJobTable, CronJobRecord, key_field="name"),
        )

    def upsert(self, cron_job: CronJobRecord, *, workspace_id: str) -> CronJobRecord:
        return self.records.upsert(
            cron_job,
            key=cron_job.name,
            workspace_id=workspace_id,
            name=cron_job.name,
            status="enabled" if cron_job.enabled else "disabled",
        )

    def get(self, name: str, *, workspace_id: str) -> CronJobRecord | None:
        return self.records.get(name, workspace_id=workspace_id)

    def list(self, *, workspace_id: str) -> list[CronJobRecord]:
        return self.records.list(workspace_id=workspace_id)

    def list_across_workspaces(self) -> list[CronJobRecord]:
        """Scheduler-owned listing over every workspace's cron jobs."""
        return self.records.list_across_workspaces()

    def due_across_workspaces(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[CronJobRecord]:
        if limit <= 0:
            return []
        statement = (
            select(CronJobTable)
            .where(
                CronJobTable.enabled.is_(True),
                CronJobTable.next_run_at.is_not(None),
                CronJobTable.next_run_at <= now,
            )
            .order_by(
                CronJobTable.next_run_at.asc().nulls_first(),
                CronJobTable.id.asc(),
            )
            .limit(limit)
        )
        return [
            CronJobRecord.model_validate(row.payload) for row in self.session.scalars(statement)
        ]
