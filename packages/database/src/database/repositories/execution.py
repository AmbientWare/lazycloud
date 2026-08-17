from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from database.mappers.apps import app_record_from_table
from database.mappers.execution import pod_url_record_from_table
from database.records.apps import AppRecord, StubRecord
from database.records.execution import PodUrlRecord
from database.repositories.common import (
    GlobalTableRepository,
    TableRepositoryConfig,
    WorkspaceTableRepository,
)
from database.tables.apps import AppTable, DeploymentTable, StubTable
from database.tables.execution import (
    CronJobRunTable,
    EventTable,
    LogTable,
    PodProcessTable,
    PodUrlTable,
    QueueMessageTable,
    TaskAttemptTable,
    TaskDependencyTable,
    TaskTable,
)
from database.tables.orchestration import ContainerTable
from pydantic import BaseModel, field_validator
from shared.containers import ContainerRecord
from shared.cron import CronJobRun
from shared.deployment_records import Deployment
from shared.deployments import StubKind
from shared.events import Event
from shared.logs import LogEntry
from shared.queue_messages import QueueMessage
from shared.realtime.streams import LogStreamQuery
from shared.tasks import (
    IN_FLIGHT_TASK_STATUSES,
    Task,
    TaskAttempt,
    TaskDependency,
    TaskStatus,
)
from shared.worker_events import CONTAINER_EVENT_RESOURCE_TYPE, TASK_EVENT_RESOURCE_TYPE
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Select,
    String,
    and_,
    case,
    cast,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified


class PodProcessRecord(BaseModel):
    id: str
    container_id: str
    pid: int
    task_id: str = ""
    command: str = ""
    status: TaskStatus = TaskStatus.Running
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class TaskDurationSample(BaseModel):
    """One finished task run's timing columns, read without the row payload."""

    created_at: datetime
    started_at: datetime
    finished_at: datetime
    status: TaskStatus

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc_datetime(value)


class RelatedTaskRecord(BaseModel):
    task: Task
    app: AppRecord | None = None
    workload: StubRecord | None = None
    deployment: Deployment | None = None
    container: ContainerRecord | None = None


class RelatedTaskPage(BaseModel):
    data: list[RelatedTaskRecord]
    next: str = ""


DEFAULT_DURATION_SAMPLE_LIMIT = 10_000


@dataclass(slots=True)
class TaskRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[Task]:
        return WorkspaceTableRepository(self.session, TableRepositoryConfig(TaskTable, Task))

    def upsert(self, task: Task, *, workspace_id: str | None = None) -> Task:
        """System-authority write keyed by task id; ownership comes from the record."""
        return self.records.upsert_across_workspaces(
            task,
            workspace_id=workspace_id or task.workspace_id,
            name=task.name,
            status=task.status.value,
        )

    def get(self, task_id: str, *, workspace_id: str) -> Task | None:
        return self.records.get(task_id, workspace_id=workspace_id)

    def get_across_workspaces(self, task_id: str) -> Task | None:
        """System lookup for scheduler/worker/runner paths acting on placed work."""
        return self.records.get_across_workspaces(task_id)

    def get_for_update_across_workspaces(self, task_id: str) -> Task | None:
        """System claim path; locks the row regardless of owning workspace."""
        statement = select(TaskTable).where(TaskTable.id == task_id).with_for_update()
        row = self.session.scalars(statement).first()
        return Task.model_validate(row.payload) if row is not None else None

    def mark_claimable(self, task_id: str, *, at: datetime) -> Task | None:
        """Record that this task's inputs have resolved, once.

        A second caller is answered with the task unchanged rather than a refusal:
        several upstreams finishing together all try to release the same dependent,
        and only one of them can be first. Returns `None` when the task is gone.
        """

        row = self.session.scalars(
            select(TaskTable).where(TaskTable.id == task_id).with_for_update()
        ).first()
        if row is None:
            return None
        task = Task.model_validate(row.payload)
        if task.claimable_at is not None:
            return task
        task.claimable_at = at
        return self.upsert(task)

    def claim_for_stub(
        self,
        stub_id: str,
        *,
        container_id: str,
        limit: int,
    ) -> list[Task]:
        """Take up to `limit` runnable tasks for this stub, binding them to a container.

        `SKIP LOCKED` is what makes two containers asking at once safe: the loser
        steps over the row the winner is holding rather than blocking behind it or
        taking it twice. Writing `container_id` inside the same transaction *is*
        the claim — it is the field every later reader already treats as ownership,
        so a claim and a pre-assignment are indistinguishable downstream.

        Only tasks whose inputs have resolved are visible here, so a dependent
        cannot be picked up before the results it is waiting on exist.
        """

        if limit <= 0:
            return []
        rows = (
            self.session.scalars(
                select(TaskTable)
                .where(
                    TaskTable.stub_id == stub_id,
                    TaskTable.status == TaskStatus.Pending.value,
                    TaskTable.container_id.is_(None),
                    TaskTable.claimable_at.is_not(None),
                )
                .order_by(TaskTable.claimable_at, TaskTable.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
            .unique()
            .all()
        )
        claimed: list[Task] = []
        for row in rows:
            task = Task.model_validate(row.payload)
            task.container_id = container_id
            claimed.append(self.upsert(task))
        return claimed

    def list(
        self,
        *,
        workspace_id: str,
        status: str | None = None,
    ) -> list[Task]:
        return self.records.list(status=status, workspace_id=workspace_id)

    def list_across_workspaces(self, *, status: str | None = None) -> list[Task]:
        """System listing for reconcilers and schedulers over every workspace."""
        return self.records.list_across_workspaces(status=status)

    def list_inflight_for_stubs(
        self,
        *,
        workspace_id: str,
        stub_ids: set[str],
    ) -> list[Task]:
        if not stub_ids:
            return []
        statement = select(TaskTable).where(
            TaskTable.workspace_id == workspace_id,
            TaskTable.stub_id.in_(stub_ids),
            TaskTable.status.in_(status.value for status in IN_FLIGHT_TASK_STATUSES),
        )
        return [Task.model_validate(row.payload) for row in self.session.scalars(statement)]

    def page_with_related(
        self,
        *,
        workspace_id: str,
        status: TaskStatus | None = None,
        app_id: str | None = None,
        deployment_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
        kind: StubKind | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        search: str | None = None,
        root_only: bool = False,
        task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> RelatedTaskPage:
        """Read a filtered task page with its owning resource context in SQL."""
        statement = (
            select(
                TaskTable.payload,
                AppTable,
                StubTable.payload,
                DeploymentTable.payload,
                ContainerTable.payload,
            )
            .select_from(TaskTable)
            .outerjoin(AppTable, AppTable.id == TaskTable.app_id)
            .outerjoin(StubTable, StubTable.id == TaskTable.stub_id)
            .outerjoin(DeploymentTable, DeploymentTable.id == TaskTable.deployment_id)
            .outerjoin(ContainerTable, ContainerTable.id == TaskTable.container_id)
            .where(TaskTable.workspace_id == workspace_id)
        )
        if status is not None:
            statement = statement.where(TaskTable.status == status.value)
        if app_id is not None:
            statement = statement.where(TaskTable.app_id == app_id)
        if deployment_id is not None:
            statement = statement.where(TaskTable.deployment_id == deployment_id)
        if stub_ids:
            statement = statement.where(TaskTable.stub_id.in_(stub_ids))
        if kind is not None:
            statement = statement.where(StubTable.type == kind.value)
        if created_after is not None:
            statement = statement.where(TaskTable.created_at >= created_after)
        if created_before is not None:
            statement = statement.where(TaskTable.created_at <= created_before)
        if search:
            pattern = f"%{search.strip()}%"
            statement = statement.where(
                or_(
                    TaskTable.name.ilike(pattern),
                    cast(TaskTable.id, String).ilike(pattern),
                )
            )
        if root_only:
            statement = statement.where(
                or_(TaskTable.root_task_id.is_(None), TaskTable.root_task_id == TaskTable.id)
            )
        if task_id is not None:
            if not _is_uuid_text(task_id):
                return RelatedTaskPage(data=[])
            statement = statement.where(TaskTable.id == task_id)

        statement = (
            statement.order_by(TaskTable.created_at.desc(), TaskTable.id.desc())
            .offset(max(offset, 0))
            .limit(max(limit, 1) + 1)
        )
        rows = list(self.session.execute(statement).tuples())
        page_rows = rows[: max(limit, 1)]
        data: list[RelatedTaskRecord] = []
        for task_payload, app_row, stub_payload, deployment_payload, container_payload in page_rows:
            data.append(
                RelatedTaskRecord(
                    task=Task.model_validate(task_payload),
                    app=app_record_from_table(app_row) if app_row is not None else None,
                    workload=StubRecord.model_validate(stub_payload) if stub_payload else None,
                    deployment=(
                        Deployment.model_validate(deployment_payload)
                        if deployment_payload
                        else None
                    ),
                    container=(
                        ContainerRecord.model_validate(container_payload)
                        if container_payload
                        else None
                    ),
                )
            )
        return RelatedTaskPage(
            data=data,
            next=str(max(offset, 0) + len(page_rows)) if len(rows) > len(page_rows) else "",
        )

    def get_with_related(self, task_id: str, *, workspace_id: str) -> RelatedTaskRecord | None:
        page = self.page_with_related(
            workspace_id=workspace_id,
            task_id=task_id,
            limit=1,
        )
        return page.data[0] if page.data else None

    def ids_for_container(self, container_id: str) -> list[str]:
        """Ids of tasks bound to a container via the indexed column or kwargs."""
        conditions = [_task_json_text(self.session, "kwargs", "container_id") == container_id]
        if _is_uuid_text(container_id):
            conditions.append(TaskTable.container_id == container_id)
        statement = select(TaskTable.id).where(or_(*conditions))
        return [str(value) for value in self.session.scalars(statement)]

    def duration_samples(
        self,
        *,
        workspace_id: str,
        stub_ids: tuple[str, ...] = (),
        deployment_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = DEFAULT_DURATION_SAMPLE_LIMIT,
    ) -> list[TaskDurationSample]:
        """SQL-windowed timing rows for runs that started and finished.

        Filters in SQL on the `ix_tasks_stub_created`/`ix_tasks_workspace_created`
        indexes, keeps the most recent `limit` rows in the window, and returns
        them in ascending `created_at` order for the caller to bucket.
        """
        statement = select(
            TaskTable.created_at,
            TaskTable.started_at,
            TaskTable.finished_at,
            TaskTable.status,
        ).where(
            TaskTable.workspace_id == workspace_id,
            TaskTable.started_at.is_not(None),
            TaskTable.finished_at.is_not(None),
        )
        if stub_ids:
            statement = statement.where(TaskTable.stub_id.in_(stub_ids))
        if deployment_id is not None:
            statement = statement.where(TaskTable.deployment_id == deployment_id)
        if start is not None:
            statement = statement.where(TaskTable.created_at >= start)
        if end is not None:
            statement = statement.where(TaskTable.created_at <= end)
        statement = statement.order_by(TaskTable.created_at.desc(), TaskTable.id.desc()).limit(
            max(limit, 1)
        )
        rows = list(self.session.execute(statement).mappings())
        rows.reverse()
        return [TaskDurationSample.model_validate(row) for row in rows]


def _utc_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_uuid_text(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


@dataclass(slots=True)
class TaskAttemptRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[TaskAttempt]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(TaskAttemptTable, TaskAttempt),
        )

    def create(self, attempt: TaskAttempt) -> TaskAttempt:
        """System-authority write; ownership comes from the attempt record."""
        return self.records.create_across_workspaces(
            attempt.model_dump(mode="json", exclude={"id"}),
            status=attempt.status.value,
        )

    def upsert(self, attempt: TaskAttempt) -> TaskAttempt:
        """System-authority write keyed by attempt id; ownership comes from the record."""
        return self.records.upsert_across_workspaces(
            attempt,
            workspace_id=attempt.workspace_id,
            status=attempt.status.value,
        )

    def list_for_task(self, task_id: str) -> list[TaskAttempt]:
        statement = (
            select(TaskAttemptTable)
            .where(TaskAttemptTable.task_id == task_id)
            .order_by(TaskAttemptTable.attempt_number.asc(), TaskAttemptTable.created_at.asc())
        )
        return [TaskAttempt.model_validate(row.payload) for row in self.session.scalars(statement)]

    def latest_for_task(self, task_id: str) -> TaskAttempt | None:
        statement = (
            select(TaskAttemptTable)
            .where(TaskAttemptTable.task_id == task_id)
            .order_by(TaskAttemptTable.attempt_number.desc(), TaskAttemptTable.created_at.desc())
            .limit(1)
        )
        row = self.session.scalars(statement).first()
        return TaskAttempt.model_validate(row.payload) if row is not None else None


@dataclass(slots=True)
class TaskDependencyRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[TaskDependency]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(TaskDependencyTable, TaskDependency),
        )

    def create(self, dependency: TaskDependency) -> TaskDependency:
        """System-authority write; ownership comes from the dependency record."""
        return self.records.create_across_workspaces(
            dependency.model_dump(mode="json", exclude={"id"}),
        )

    def list_for_task(self, task_id: str) -> list[TaskDependency]:
        statement = (
            select(TaskDependencyTable)
            .where(TaskDependencyTable.task_id == task_id)
            .order_by(TaskDependencyTable.created_at.asc(), TaskDependencyTable.id.asc())
        )
        return [
            TaskDependency.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def list_for_upstream(self, upstream_task_id: str) -> list[TaskDependency]:
        statement = (
            select(TaskDependencyTable)
            .where(TaskDependencyTable.upstream_task_id == upstream_task_id)
            .order_by(TaskDependencyTable.created_at.asc(), TaskDependencyTable.id.asc())
        )
        return [
            TaskDependency.model_validate(row.payload) for row in self.session.scalars(statement)
        ]

    def list_for_root(
        self,
        root_task_id: str,
        *,
        workspace_id: str | None = None,
    ) -> list[TaskDependency]:
        statement = select(TaskDependencyTable).where(
            TaskDependencyTable.root_task_id == root_task_id
        )
        if workspace_id is not None:
            statement = statement.where(TaskDependencyTable.workspace_id == workspace_id)
        statement = statement.order_by(
            TaskDependencyTable.created_at.asc(),
            TaskDependencyTable.id.asc(),
        )
        return [
            TaskDependency.model_validate(row.payload) for row in self.session.scalars(statement)
        ]


@dataclass(slots=True)
class LogRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[LogEntry]:
        return WorkspaceTableRepository(self.session, TableRepositoryConfig(LogTable, LogEntry))

    def append(self, entry: LogEntry, *, workspace_id: str | None = None) -> LogEntry:
        """System-authority write; runner/worker logs may be cluster-level."""
        return self.records.upsert_across_workspaces(entry, workspace_id=workspace_id)

    def list_for_task(self, task_id: str) -> list[LogEntry]:
        """System listing filtered to a task the caller already authorized."""
        statement = (
            select(LogTable)
            .where(LogTable.task_id == task_id)
            .order_by(LogTable.created_at.asc(), LogTable.id.asc())
        )
        return [LogEntry.model_validate(row.payload) for row in self.session.scalars(statement)]

    def list_across_workspaces(self, query: LogStreamQuery | None = None) -> list[LogEntry]:
        """System/admin log stream over every workspace; operator surfaces only."""
        entries = self.records.list_across_workspaces()
        if query is None:
            entries.sort(key=lambda item: item.created_at)
            return entries
        if query.task_id:
            entries = [item for item in entries if item.task_id == query.task_id]
        if query.object_type == "task" and query.object_id:
            entries = [item for item in entries if item.task_id == query.object_id]
        if query.container_id:
            tasks = {
                task.id: task
                for task in TaskRepository(self.session).records.list_across_workspaces()
            }
            entries = [
                item
                for item in entries
                if _task_container_id(tasks.get(item.task_id)) == query.container_id
            ]
        if query.query:
            needle = query.query.lower()
            entries = [item for item in entries if needle in item.message.lower()]
        if query.start_time is not None:
            entries = [item for item in entries if item.created_at >= query.start_time]
        if query.end_time is not None:
            entries = [item for item in entries if item.created_at < query.end_time]
        entries.sort(key=lambda item: item.created_at)
        return entries


def _task_container_id(task: Task | None) -> str:
    if task is None:
        return ""
    return str(task.kwargs.get("container_id") or "")


@dataclass(slots=True)
class EventRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[Event]:
        return WorkspaceTableRepository(self.session, TableRepositoryConfig(EventTable, Event))

    def append(self, event: Event, *, workspace_id: str | None = None) -> Event:
        """System-authority write; cluster-level events carry no workspace."""
        return self.records.upsert_across_workspaces(event, workspace_id=workspace_id)

    def list(
        self,
        *,
        workspace_id: str,
        include_cluster: bool = False,
        resource_type: str | None = None,
        resource_id: str | None = None,
        actions: Sequence[str] | None = None,
        container_id: str | None = None,
        related_task_ids: Sequence[str] = (),
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Event]:
        """SQL-filtered workspace events in descending `created_at` order.

        Container scope runs one indexed query per match branch (resource row,
        payload container id, related task rows) and merges the pages: the
        single OR form defeats the planner's index selection under
        ORDER BY + LIMIT.
        """
        return self._list(
            workspace_id=workspace_id,
            include_cluster=include_cluster,
            resource_type=resource_type,
            resource_id=resource_id,
            actions=actions,
            container_id=container_id,
            related_task_ids=related_task_ids,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )

    def list_across_workspaces(
        self,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        actions: Sequence[str] | None = None,
        container_id: str | None = None,
        related_task_ids: Sequence[str] = (),
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Event]:
        """Admin/system event stream over every workspace; operator surfaces only."""
        return self._list(
            workspace_id=None,
            include_cluster=True,
            resource_type=resource_type,
            resource_id=resource_id,
            actions=actions,
            container_id=container_id,
            related_task_ids=related_task_ids,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )

    def count(
        self,
        *,
        workspace_id: str,
        include_cluster: bool = False,
        resource_type: str | None = None,
        resource_id: str | None = None,
        actions: Sequence[str] | None = None,
        container_id: str | None = None,
        related_task_ids: Sequence[str] = (),
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        return self._count(
            workspace_id=workspace_id,
            include_cluster=include_cluster,
            resource_type=resource_type,
            resource_id=resource_id,
            actions=actions,
            container_id=container_id,
            related_task_ids=related_task_ids,
            since=since,
            until=until,
        )

    def count_across_workspaces(
        self,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        actions: Sequence[str] | None = None,
        container_id: str | None = None,
        related_task_ids: Sequence[str] = (),
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        """Admin/system event count over every workspace; operator surfaces only."""
        return self._count(
            workspace_id=None,
            include_cluster=True,
            resource_type=resource_type,
            resource_id=resource_id,
            actions=actions,
            container_id=container_id,
            related_task_ids=related_task_ids,
            since=since,
            until=until,
        )

    def _list(
        self,
        *,
        workspace_id: str | None,
        include_cluster: bool,
        resource_type: str | None,
        resource_id: str | None,
        actions: Sequence[str] | None,
        container_id: str | None,
        related_task_ids: Sequence[str],
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
        offset: int,
    ) -> list[Event]:
        if limit is not None and limit <= 0:
            return []
        base = self._filtered(
            select(EventTable),
            workspace_id=workspace_id,
            include_cluster=include_cluster,
            resource_type=resource_type,
            resource_id=resource_id,
            actions=actions,
            since=since,
            until=until,
        )
        if container_id is not None:
            events = self._merged_container_branches(
                base,
                container_id=container_id,
                related_task_ids=related_task_ids,
                fetch=None if limit is None else offset + limit,
            )
            end = None if limit is None else offset + limit
            return events[offset:end]
        statement = base.order_by(EventTable.created_at.desc(), EventTable.id.asc())
        if offset > 0:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return [Event.model_validate(row.payload) for row in self.session.scalars(statement)]

    def _count(
        self,
        *,
        workspace_id: str | None,
        include_cluster: bool,
        resource_type: str | None,
        resource_id: str | None,
        actions: Sequence[str] | None,
        container_id: str | None,
        related_task_ids: Sequence[str],
        since: datetime | None,
        until: datetime | None,
    ) -> int:
        statement = self._filtered(
            select(func.count()).select_from(EventTable),
            workspace_id=workspace_id,
            include_cluster=include_cluster,
            resource_type=resource_type,
            resource_id=resource_id,
            actions=actions,
            since=since,
            until=until,
        )
        if container_id is not None:
            statement = statement.where(
                or_(*self._container_conditions(container_id, related_task_ids))
            )
        return int(self.session.execute(statement).scalar_one())

    def prune(
        self,
        *,
        older_than: datetime,
        actions: Sequence[str] | None = None,
    ) -> int:
        statement = delete(EventTable).where(EventTable.created_at < older_than)
        if actions is not None:
            statement = statement.where(EventTable.action.in_(tuple(actions)))
        result = self.session.execute(statement)
        self.session.flush()
        return int(result.rowcount) if isinstance(result, CursorResult) else 0

    def _filtered[TSelect: Select[tuple[EventTable]] | Select[tuple[int]]](
        self,
        statement: TSelect,
        *,
        workspace_id: str | None,
        include_cluster: bool,
        resource_type: str | None,
        resource_id: str | None,
        actions: Sequence[str] | None,
        since: datetime | None,
        until: datetime | None,
    ) -> TSelect:
        if workspace_id is not None:
            scope = EventTable.workspace_id == workspace_id
            if include_cluster:
                scope = or_(scope, EventTable.workspace_id.is_(None))
            statement = statement.where(scope)
        if resource_type is not None:
            statement = statement.where(EventTable.resource_type == resource_type)
        if resource_id is not None:
            statement = statement.where(EventTable.resource_id == resource_id)
        if actions is not None:
            statement = statement.where(EventTable.action.in_(tuple(actions)))
        if since is not None:
            statement = statement.where(EventTable.created_at >= since)
        if until is not None:
            statement = statement.where(EventTable.created_at <= until)
        return statement

    def _container_conditions(
        self,
        container_id: str,
        related_task_ids: Sequence[str],
    ) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [
            and_(
                EventTable.resource_type == CONTAINER_EVENT_RESOURCE_TYPE,
                EventTable.resource_id == container_id,
            ),
            _event_json_text(self.session, "data", "container_id") == container_id,
        ]
        if related_task_ids:
            conditions.append(
                and_(
                    EventTable.resource_type == TASK_EVENT_RESOURCE_TYPE,
                    EventTable.resource_id.in_(tuple(related_task_ids)),
                )
            )
        return conditions

    def _merged_container_branches(
        self,
        base: Select[tuple[EventTable]],
        *,
        container_id: str,
        related_task_ids: Sequence[str],
        fetch: int | None,
    ) -> list[Event]:
        merged: dict[str, tuple[datetime, str, Event]] = {}
        for condition in self._container_conditions(container_id, related_task_ids):
            statement = base.where(condition).order_by(
                EventTable.created_at.desc(),
                EventTable.id.asc(),
            )
            if fetch is not None:
                statement = statement.limit(fetch)
            for row in self.session.scalars(statement):
                merged[row.id] = (row.created_at, row.id, Event.model_validate(row.payload))
        ranked = sorted(merged.values(), key=lambda item: item[1])
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [event for _, _, event in ranked]


def _task_json_text(session: Session, *path: str) -> ColumnElement[str]:
    if session.get_bind().dialect.name == "postgresql":
        return func.jsonb_extract_path_text(TaskTable.payload, *path, type_=String)
    return func.json_extract(TaskTable.payload, "$." + ".".join(path), type_=String)


def _event_json_text(session: Session, *path: str) -> ColumnElement[str]:
    if session.get_bind().dialect.name == "postgresql":
        return func.jsonb_extract_path_text(EventTable.payload, *path, type_=String)
    return func.json_extract(EventTable.payload, "$." + ".".join(path), type_=String)


@dataclass(slots=True)
class QueueRepository:
    session: Session

    @property
    def messages(self) -> WorkspaceTableRepository[QueueMessage]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(QueueMessageTable, QueueMessage),
        )

    def upsert_message(
        self,
        message: QueueMessage,
        *,
        workspace_id: str,
    ) -> QueueMessage:
        return self.messages.upsert(message, workspace_id=workspace_id)

    def delete_messages_for_queues(
        self,
        *,
        workspace_id: str,
        queues: set[str],
    ) -> int:
        if not queues:
            return 0
        result = self.session.execute(
            delete(QueueMessageTable).where(
                QueueMessageTable.workspace_id == workspace_id,
                QueueMessageTable.queue.in_(queues),
            )
        )
        return int(result.rowcount) if isinstance(result, CursorResult) else 0

    def claim_available_message(
        self,
        queue: str,
        *,
        workspace_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> QueueMessage | None:
        statement = (
            select(QueueMessageTable)
            .where(
                QueueMessageTable.workspace_id == workspace_id,
                QueueMessageTable.queue == queue,
                QueueMessageTable.available_at <= now,
                or_(
                    QueueMessageTable.leased_until.is_(None),
                    QueueMessageTable.leased_until <= now,
                ),
            )
            .order_by(QueueMessageTable.created_at, QueueMessageTable.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        row = self.session.scalars(statement).first()
        if row is None:
            return None
        return self._lease_message(row, lease_until=lease_until)

    def claim_expired_message(
        self,
        queue: str,
        *,
        workspace_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> QueueMessage | None:
        statement = (
            select(QueueMessageTable)
            .where(
                QueueMessageTable.workspace_id == workspace_id,
                QueueMessageTable.queue == queue,
                QueueMessageTable.expires_at.is_not(None),
                QueueMessageTable.expires_at <= now,
                QueueMessageTable.leased_until.is_(None),
            )
            .order_by(QueueMessageTable.expires_at, QueueMessageTable.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        row = self.session.scalars(statement).first()
        if row is None:
            return None
        return self._lease_message(row, lease_until=lease_until)

    def queue_depth(self, queue: str, *, workspace_id: str, now: datetime) -> int:
        statement = (
            select(func.count())
            .select_from(QueueMessageTable)
            .where(
                QueueMessageTable.workspace_id == workspace_id,
                QueueMessageTable.queue == queue,
                or_(
                    QueueMessageTable.expires_at.is_(None),
                    QueueMessageTable.expires_at > now,
                ),
            )
        )
        return int(self.session.scalar(statement) or 0)

    def oldest_pending_at(
        self,
        queue: str,
        *,
        workspace_id: str,
        now: datetime,
    ) -> datetime | None:
        statement = select(func.min(QueueMessageTable.created_at)).where(
            QueueMessageTable.workspace_id == workspace_id,
            QueueMessageTable.queue == queue,
            QueueMessageTable.available_at <= now,
            or_(
                QueueMessageTable.leased_until.is_(None),
                QueueMessageTable.leased_until <= now,
            ),
            or_(
                QueueMessageTable.expires_at.is_(None),
                QueueMessageTable.expires_at > now,
            ),
        )
        oldest = self.session.scalar(statement)
        return _utc_datetime(oldest) if oldest is not None else None

    def renew_message_lease(
        self,
        message_id: str,
        *,
        workspace_id: str,
        lease_until: datetime,
    ) -> bool:
        statement = (
            select(QueueMessageTable)
            .where(
                QueueMessageTable.id == message_id,
                QueueMessageTable.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        row = self.session.scalars(statement).first()
        if row is None:
            return False
        message = QueueMessage.model_validate(row.payload)
        message.leased_until = lease_until
        row.leased_until = lease_until
        row.payload = message.model_dump(mode="json")
        flag_modified(row, "payload")
        self.session.flush()
        return True

    def _lease_message(
        self,
        row: QueueMessageTable,
        *,
        lease_until: datetime,
    ) -> QueueMessage:
        message = QueueMessage.model_validate(row.payload)
        message.attempts = row.attempts + 1
        message.leased_until = lease_until
        row.attempts = message.attempts
        row.leased_until = lease_until
        row.payload = message.model_dump(mode="json")
        flag_modified(row, "payload")
        self.session.flush()
        return message


@dataclass(slots=True)
class PodUrlRepository:
    session: Session

    def upsert(self, *, container_id: str, port: int, url: str) -> PodUrlRecord:
        now = datetime.now(UTC)
        values: dict[str, str | int | datetime] = {
            "id": str(uuid4()),
            "container_id": container_id,
            "port": port,
            "url": url,
            "created_at": now,
            "updated_at": now,
        }
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            insert = postgresql_insert(PodUrlTable).values(**values)
        elif dialect == "sqlite":
            insert = sqlite_insert(PodUrlTable).values(**values)
        else:
            raise RuntimeError("pod URLs require PostgreSQL or SQLite")
        statement = (
            insert.on_conflict_do_update(
                index_elements=[PodUrlTable.container_id, PodUrlTable.port],
                set_={
                    "url": insert.excluded.url,
                    "updated_at": case(
                        (PodUrlTable.url == insert.excluded.url, PodUrlTable.updated_at),
                        (PodUrlTable.updated_at < now, now),
                        else_=PodUrlTable.updated_at,
                    ),
                },
            )
            .returning(PodUrlTable)
            .execution_options(populate_existing=True)
        )
        return pod_url_record_from_table(self.session.scalars(statement).one())

    def get(self, *, container_id: str, port: int) -> PodUrlRecord | None:
        row = self.session.scalars(
            select(PodUrlTable).where(
                PodUrlTable.container_id == container_id,
                PodUrlTable.port == port,
            )
        ).first()
        return pod_url_record_from_table(row) if row is not None else None

    def list_for_container(self, container_id: str) -> list[PodUrlRecord]:
        statement = (
            select(PodUrlTable)
            .where(PodUrlTable.container_id == container_id)
            .order_by(PodUrlTable.port, PodUrlTable.id)
        )
        return [pod_url_record_from_table(row) for row in self.session.scalars(statement)]

    def list_for_stub(self, *, stub_id: str, workspace_id: str) -> list[PodUrlRecord]:
        statement = (
            select(PodUrlTable)
            .join(ContainerTable, ContainerTable.id == PodUrlTable.container_id)
            .where(
                ContainerTable.stub_id == stub_id,
                ContainerTable.workspace_id == workspace_id,
            )
            .order_by(PodUrlTable.container_id, PodUrlTable.port, PodUrlTable.id)
        )
        return [pod_url_record_from_table(row) for row in self.session.scalars(statement)]


@dataclass(slots=True)
class PodExecutionRepository:
    session: Session

    @property
    def processes(self) -> GlobalTableRepository[PodProcessRecord]:
        return GlobalTableRepository(
            self.session,
            TableRepositoryConfig(PodProcessTable, PodProcessRecord),
        )

    @property
    def urls(self) -> PodUrlRepository:
        return PodUrlRepository(self.session)


@dataclass(slots=True)
class CronJobRunRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[CronJobRun]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(CronJobRunTable, CronJobRun),
        )
