from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from database.mappers.apps import (
    app_container_shutdown_intent_from_table,
    app_deployment_intent_from_table,
    app_record_from_table,
    stub_from_table,
    write_app_row,
    write_stub_row,
)
from database.mappers.containers import container_from_row
from database.records.apps import (
    AppContainerShutdownIntentRecord,
    AppDeploymentIntentRecord,
    AppRecord,
    AutoscalingStubConfig,
    AutoscalingStubRecord,
    AutoscalingStubRuntimeConfig,
    StubPower,
    StubRecord,
)
from database.repositories.common import (
    bucket_index,
    names_by_id,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import container_storage_release_pending
from database.tables.apps import (
    AppContainerShutdownIntentTable,
    AppDeploymentIntentTable,
    AppTable,
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
from shared.containers import ContainerStatus
from shared.deployments import StubKind
from shared.disks import DISK_ROOT_MOUNT_PATH
from shared.enums import StringEnum
from shared.errors import ConflictError, NotFoundError
from shared.tasks import TaskStatus
from shared.workload_config import StubAutoscalerConfig, StubTaskPolicy
from sqlalchemy import (
    case,
    cast,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    type_coerce,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.orm import Session, load_only

_ROOT_DISK_PATH = f'$[*] ? (!exists(@.mount_path) || @.mount_path == "{DISK_ROOT_MOUNT_PATH}")'


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

    def lock_name(self, *, workspace_id: str, name: str) -> None:
        # A row lock cannot serialize concurrent creation before the app exists.
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"app-name:{workspace_id}:{name}"},
        )

    def latest_by_name_across_workspaces(self, name: str) -> AppRecord | None:
        row = self.session.scalars(
            select(AppTable)
            .where(AppTable.name == name, AppTable.deleted_at.is_(None))
            .order_by(AppTable.version.desc(), AppTable.updated_at.desc())
            .limit(1)
        ).first()
        return app_record_from_table(row) if row is not None else None

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

    def active_by_ids(
        self, app_ids: Sequence[str], *, workspace_id: str | None = None
    ) -> dict[str, bool]:
        wanted = tuple(dict.fromkeys(app_ids))
        active = {app_id: False for app_id in wanted}
        if not wanted:
            return active
        statement = select(AppTable.id).where(
            AppTable.id.in_(wanted),
            AppTable.lifecycle_state == AppLifecycleState.Active.value,
            AppTable.deleted_at.is_(None),
        )
        if workspace_id is not None:
            statement = statement.where(AppTable.workspace_id == workspace_id)
        rows = self.session.scalars(statement)
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

    def lock_lifecycle_state(self, app_id: str, *, workspace_id: str) -> AppLifecycleState | None:
        state = self.session.scalar(
            select(AppTable.lifecycle_state)
            .where(
                AppTable.id == app_id,
                AppTable.workspace_id == workspace_id,
                AppTable.deleted_at.is_(None),
            )
            .with_for_update(read=True)
        )
        return AppLifecycleState(state) if state is not None else None

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
        active: bool | None = None,
    ) -> list[AppRecord]:
        statement = select(AppTable).where(AppTable.workspace_id == workspace_id)
        if not include_deleted:
            statement = statement.where(AppTable.deleted_at.is_(None))
        if active is not None:
            statement = statement.where(
                (AppTable.lifecycle_state == AppLifecycleState.Active.value).is_(active)
            )
        statement = statement.order_by(AppTable.name, AppTable.version, AppTable.id)
        return [app_record_from_table(row) for row in self.session.scalars(statement)]

    def list_across_workspaces(
        self, *, include_deleted: bool = False, active: bool | None = None
    ) -> list[AppRecord]:
        statement = select(AppTable)
        if not include_deleted:
            statement = statement.where(AppTable.deleted_at.is_(None))
        if active is not None:
            statement = statement.where(
                (AppTable.lifecycle_state == AppLifecycleState.Active.value).is_(active)
            )
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
        exclude_ids: Sequence[str] = (),
    ) -> list[AppRecord]:
        states = tuple(state.value for state in UNFINISHED_APP_LIFECYCLE_STATES)
        statement = (
            select(AppTable)
            .where(
                AppTable.lifecycle_state.in_(states),
                AppTable.id.not_in(exclude_ids),
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

    def capture(
        self,
        *,
        app_id: str,
        workspace_id: str,
        operation_revision: int,
        target: AppDeploymentIntentTarget,
        active_only: bool,
    ) -> None:
        self.clear(app_id=app_id)
        selected = select(
            DeploymentTable.app_id,
            DeploymentTable.id,
            literal(operation_revision),
            literal(target.value),
        ).where(
            DeploymentTable.workspace_id == workspace_id,
            DeploymentTable.app_id == app_id,
            DeploymentTable.deleted_at.is_(None),
        )
        if active_only:
            selected = selected.where(DeploymentTable.active.is_(True))
        self.session.execute(
            insert(AppDeploymentIntentTable).from_select(
                ["app_id", "deployment_id", "operation_revision", "target"], selected
            )
        )

    def retarget(
        self, *, app_id: str, operation_revision: int, target: AppDeploymentIntentTarget
    ) -> None:
        self.session.execute(
            update(AppDeploymentIntentTable)
            .where(AppDeploymentIntentTable.app_id == app_id)
            .values(
                operation_revision=operation_revision,
                target=target.value,
                event_id=None,
                event_created_at=None,
                workspace_change_published_at=None,
            )
        )

    def list(self, *, app_id: str) -> list[AppDeploymentIntentRecord]:
        rows = self.session.scalars(
            select(AppDeploymentIntentTable)
            .where(AppDeploymentIntentTable.app_id == app_id)
            .order_by(AppDeploymentIntentTable.deployment_id)
        )
        return [app_deployment_intent_from_table(row) for row in rows]

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

    def capture_pending_shutdowns(
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
            container_from_row(row)
            for row in self.session.scalars(
                select(ContainerTable).where(
                    ContainerTable.workspace_id == workspace_id,
                    ContainerTable.app_id == app_id,
                    container_storage_release_pending(),
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

    def callback_url(self, stub_id: str, *, workspace_id: str) -> str | None:
        return self.session.scalar(
            select(StubTable.configuration["callback_url"].as_string()).where(
                StubTable.id == stub_id,
                StubTable.workspace_id == workspace_id,
                StubTable.type.in_(
                    [StubKind.Function.value, StubKind.Endpoint.value, StubKind.Asgi.value]
                ),
            )
        )

    def exists(self, stub_id: str, *, workspace_id: str, kind: StubKind) -> bool:
        try:
            UUID(stub_id)
        except ValueError:
            return False
        return bool(
            self.session.scalar(
                select(
                    select(StubTable.id)
                    .where(
                        StubTable.id == stub_id,
                        StubTable.workspace_id == workspace_id,
                        StubTable.type == kind.value,
                    )
                    .exists()
                )
            )
        )

    def upsert(self, stub: StubRecord) -> StubRecord:
        WorkspaceRepository(self.session).lock_active_owner(stub.workspace_id)
        stub = StubRecord.model_validate(dict(stub))
        row = self.session.get(StubTable, stub.id)
        if row is None:
            row = StubTable(id=stub.id)
            self.session.add(row)
        elif row.workspace_id != stub.workspace_id:
            raise ConflictError("stub ownership cannot change")
        write_stub_row(row, stub)
        self.session.flush()
        return stub_from_table(row)

    def get(self, stub_id: str, *, workspace_id: str) -> StubRecord | None:
        try:
            UUID(stub_id)
        except ValueError:
            return None
        row = self.session.scalar(
            select(StubTable).where(
                StubTable.id == stub_id,
                StubTable.workspace_id == workspace_id,
            )
        )
        return stub_from_table(row) if row is not None else None

    def mounts_root_disk(self, stub_id: str, *, workspace_id: str) -> bool:
        """Whether the stub mounts a disk at `/`, answered in SQL from its disk list alone.

        A stored disk leaves `mount_path` out when it is the default `/`, so a
        disk without one is a root disk.
        """
        disks = type_coerce(StubTable.configuration, JSONB)["disks"]
        return bool(
            self.session.scalar(
                select(func.jsonb_path_exists(disks, cast(_ROOT_DISK_PATH, JSONPATH))).where(
                    StubTable.id == stub_id,
                    StubTable.workspace_id == workspace_id,
                )
            )
        )

    def get_across_workspaces(self, stub_id: str) -> StubRecord | None:
        """System lookup for scheduler/worker/runner paths resolving placed work."""
        try:
            UUID(stub_id)
        except ValueError:
            return None
        row = self.session.get(StubTable, stub_id)
        return stub_from_table(row) if row is not None else None

    def get_by_name(self, name: str, *, workspace_id: str | None) -> StubRecord | None:
        statement = select(StubTable).where(StubTable.name == name).limit(2)
        if workspace_id is not None:
            statement = statement.where(StubTable.workspace_id == workspace_id)
        rows = list(self.session.scalars(statement))
        if len(rows) > 1:
            raise ConflictError(f"stub name is ambiguous; use a stub ID: {name}")
        return stub_from_table(rows[0]) if rows else None

    def get_for_deployment(
        self, deployment_id: str, *, workspace_id: str | None
    ) -> StubRecord | None:
        statement = (
            select(StubTable)
            .join(DeploymentTable, DeploymentTable.stub_id == StubTable.id)
            .where(
                DeploymentTable.id == deployment_id,
                DeploymentTable.workspace_id == StubTable.workspace_id,
            )
        )
        if workspace_id is not None:
            statement = statement.where(StubTable.workspace_id == workspace_id)
        row = self.session.scalar(statement)
        return stub_from_table(row) if row is not None else None

    def list(
        self, *, workspace_id: str, app_id: str | None = None, deployed_only: bool = False
    ) -> list[StubRecord]:
        statement = select(StubTable).where(StubTable.workspace_id == workspace_id)
        if deployed_only:
            statement = statement.where(StubTable.deployment_id.is_not(None))
        if app_id is not None:
            statement = statement.where(StubTable.app_id == app_id)
        return [
            stub_from_table(row)
            for row in self.session.scalars(
                statement.order_by(StubTable.created_at.desc(), StubTable.id)
            )
        ]

    def list_across_workspaces(
        self, *, app_id: str | None = None, deployed_only: bool = False
    ) -> list[StubRecord]:
        statement = select(StubTable)
        if deployed_only:
            statement = statement.where(StubTable.deployment_id.is_not(None))
        if app_id is not None:
            statement = statement.where(StubTable.app_id == app_id)
        return [
            stub_from_table(row)
            for row in self.session.scalars(
                statement.order_by(StubTable.created_at.desc(), StubTable.id)
            )
        ]

    def find_reusable(
        self,
        *,
        workspace_id: str,
        preparation_fingerprint: str,
    ) -> StubRecord | None:
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"stub-preparation:{workspace_id}:{preparation_fingerprint}"},
        )
        row = self.session.scalars(
            select(StubTable)
            .where(
                StubTable.workspace_id == workspace_id,
                StubTable.preparation_fingerprint == preparation_fingerprint,
            )
            .with_for_update()
        ).first()
        return stub_from_table(row) if row is not None else None

    def set_preparation_fingerprint(
        self,
        stub_id: str,
        *,
        workspace_id: str,
        fingerprint: str | None,
    ) -> None:
        self.session.execute(
            update(StubTable)
            .where(StubTable.id == stub_id, StubTable.workspace_id == workspace_id)
            .values(preparation_fingerprint=fingerprint)
        )

    def power(self, stub_id: str, *, workspace_id: str) -> StubPower:
        row = self.session.execute(
            select(StubTable.parked, StubTable.woken_at).where(
                StubTable.id == stub_id, StubTable.workspace_id == workspace_id
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"stub not found: {stub_id}")
        return StubPower(parked=row.parked, woken_at=row.woken_at)

    def park(self, stub_id: str, *, workspace_id: str) -> None:
        """Stop the autoscaler starting anything for the stub, and forget a pending start."""
        self.session.execute(
            update(StubTable)
            .where(StubTable.id == stub_id, StubTable.workspace_id == workspace_id)
            .values(parked=True, woken_at=None)
        )

    def wake(self, stub_id: str, *, workspace_id: str, woken_at: datetime | None) -> None:
        """Let the autoscaler start the stub again; a start also asks for a container now.

        A connection wakes with no `woken_at`, and only a parked stub has
        anything to write, so the update costs no write on the proxy's path.
        """
        statement = update(StubTable).where(
            StubTable.id == stub_id, StubTable.workspace_id == workspace_id
        )
        if woken_at is None:
            self.session.execute(statement.where(StubTable.parked).values(parked=False))
            return
        self.session.execute(statement.values(parked=False, woken_at=woken_at))

    def end_start(self, stub_id: str, *, workspace_id: str, woken_at: datetime) -> None:
        """Forget the start made at `woken_at`, unless a later start has replaced it."""
        self.session.execute(
            update(StubTable)
            .where(
                StubTable.id == stub_id,
                StubTable.workspace_id == workspace_id,
                StubTable.woken_at == woken_at,
            )
            .values(woken_at=None)
        )

    def list_autoscaling_across_workspaces(
        self,
        *,
        stub_ids: Sequence[str] | None = None,
    ) -> list[AutoscalingStubRecord]:
        statement = select(StubTable).options(
            load_only(
                StubTable.id,
                StubTable.workspace_id,
                StubTable.type,
                StubTable.app_id,
                StubTable.deployment_id,
                StubTable.autoscaling_enabled,
                StubTable.parked,
                StubTable.woken_at,
                StubTable.runtime_cpu,
                StubTable.runtime_cpu_millicores,
                StubTable.runtime_gpu,
                StubTable.runtime_gpu_count,
                StubTable.runtime_timeout_seconds,
                StubTable.runtime_keep_warm,
                StubTable.runtime_workspace_gpu_quota,
                StubTable.runtime_workspace_cpu_quota_millicores,
                StubTable.autoscaler_type,
                StubTable.autoscaler_max_containers,
                StubTable.autoscaler_min_containers,
                StubTable.autoscaler_tasks_per_container,
                StubTable.autoscaler_failed_container_threshold,
                StubTable.autoscaler_max_failed_containers,
                StubTable.autoscaler_failure_threshold,
                StubTable.autoscaler_failed_container_window_seconds,
                StubTable.autoscaler_failure_window_seconds,
                StubTable.task_policy_timeout,
                StubTable.task_policy_timeout_seconds,
                StubTable.task_policy_ttl,
                StubTable.task_policy_ttl_seconds,
            )
        )
        if stub_ids is not None:
            wanted = tuple(dict.fromkeys(stub_ids))
            if not wanted:
                return []
            statement = statement.where(StubTable.id.in_(wanted))
        records: list[AutoscalingStubRecord] = []
        for row in self.session.scalars(
            statement.order_by(StubTable.created_at.desc(), StubTable.id)
        ):
            runtime_values: dict[str, JsonValue | list[str]] = {
                "cpu": row.runtime_cpu,
                "cpu_millicores": row.runtime_cpu_millicores,
                "gpu": row.runtime_gpu,
                "gpu_count": row.runtime_gpu_count,
                "timeout_seconds": row.runtime_timeout_seconds,
                "keep_warm": row.runtime_keep_warm,
                "workspace_gpu_quota": row.runtime_workspace_gpu_quota,
                "workspace_cpu_quota_millicores": row.runtime_workspace_cpu_quota_millicores,
            }
            autoscaler_values: dict[str, str | int | None] = {
                "type": row.autoscaler_type,
                "max_containers": row.autoscaler_max_containers,
                "min_containers": row.autoscaler_min_containers,
                "tasks_per_container": row.autoscaler_tasks_per_container,
                "failed_container_threshold": row.autoscaler_failed_container_threshold,
                "max_failed_containers": row.autoscaler_max_failed_containers,
                "failure_threshold": row.autoscaler_failure_threshold,
                "failed_container_window_seconds": row.autoscaler_failed_container_window_seconds,
                "failure_window_seconds": row.autoscaler_failure_window_seconds,
            }
            task_policy_values: dict[str, float | None] = {
                "timeout": row.task_policy_timeout,
                "timeout_seconds": row.task_policy_timeout_seconds,
                "ttl": row.task_policy_ttl,
                "ttl_seconds": row.task_policy_ttl_seconds,
            }
            records.append(
                AutoscalingStubRecord(
                    id=str(row.id),
                    workspace_id=str(row.workspace_id),
                    kind=StubKind(row.type),
                    app_id=row.app_id,
                    deployment_id=row.deployment_id,
                    config=AutoscalingStubConfig(
                        runtime=AutoscalingStubRuntimeConfig.model_validate(
                            {
                                key: value
                                for key, value in runtime_values.items()
                                if value is not None
                            }
                        ),
                        autoscaler=StubAutoscalerConfig.model_validate(
                            {
                                key: value
                                for key, value in autoscaler_values.items()
                                if value is not None
                            }
                        ),
                        task_policy=StubTaskPolicy.model_validate(
                            {
                                key: value
                                for key, value in task_policy_values.items()
                                if value is not None
                            }
                        ),
                        metadata={"autoscaling_enabled": row.autoscaling_enabled}
                        if row.autoscaling_enabled is not None
                        else {},
                    ),
                    power=StubPower(parked=row.parked, woken_at=row.woken_at),
                )
            )
        return records

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
        return [stub_from_table(row) for row in self.session.scalars(statement)]

    def list_for_deployments(
        self,
        deployment_ids: Sequence[str],
        *,
        workspace_id: str,
        for_update: bool = False,
    ) -> list[StubRecord]:
        wanted = tuple(dict.fromkeys(deployment_ids))
        if not wanted:
            return []
        statement = (
            select(StubTable)
            .join(DeploymentTable, DeploymentTable.stub_id == StubTable.id)
            .where(
                DeploymentTable.id.in_(wanted),
                DeploymentTable.workspace_id == workspace_id,
                StubTable.workspace_id == workspace_id,
            )
        )
        if for_update:
            statement = statement.with_for_update(of=StubTable, key_share=True)
        return [stub_from_table(row) for row in self.session.scalars(statement)]

    def get_for_update(self, stub_id: str, *, workspace_id: str) -> StubRecord | None:
        # Serialize admission without blocking concurrent container foreign-key checks.
        row = self.session.scalars(
            select(StubTable)
            .where(StubTable.id == stub_id, StubTable.workspace_id == workspace_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        ).first()
        return stub_from_table(row) if row is not None else None

    def get_by_name_for_update(self, name: str, *, workspace_id: str) -> StubRecord | None:
        rows = list(
            self.session.scalars(
                select(StubTable)
                .where(StubTable.name == name, StubTable.workspace_id == workspace_id)
                .order_by(StubTable.created_at.asc(), StubTable.id.asc())
                .limit(2)
                .with_for_update()
            )
        )
        if len(rows) > 1:
            raise ConflictError(f"stub name is ambiguous; use a stub ID: {name}")
        row = rows[0] if rows else None
        return stub_from_table(row) if row is not None else None

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
        return (
            self.session.scalar(
                delete(StubTable)
                .where(
                    StubTable.id == stub_id,
                    StubTable.workspace_id == workspace_id,
                )
                .returning(StubTable.id)
            )
            is not None
        )
