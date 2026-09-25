from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from database.mappers.containers import container_from_row
from database.mappers.execution import (
    cron_job_run_from_table,
    event_from_table,
    log_entry_from_table,
    pod_url_record_from_table,
    task_attempt_from_table,
    task_dependency_from_table,
    task_from_table,
    write_event_row,
    write_log_row,
    write_task_attempt_row,
    write_task_row,
)
from database.records.execution import PodUrlRecord
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.identity import WorkspaceRepository
from database.tables.apps import AppTable, DeploymentTable, StubTable
from database.tables.billing import BillingAccountTable
from database.tables.execution import (
    CronJobRunTable,
    EventTable,
    LogTable,
    PodUrlTable,
    TaskAttemptTable,
    TaskDependencyTable,
    TaskTable,
)
from database.tables.identity import WorkspaceMemberTable
from database.tables.orchestration import ContainerTable
from pydantic import BaseModel, field_validator
from shared.containers import ContainerRecord, ContainerStatus
from shared.cron import CronJobRun
from shared.deployment_records import DEFAULT_FUNCTION_TIMEOUT_SECONDS
from shared.deployments import StubKind
from shared.errors import ConflictError
from shared.events import Event
from shared.logs import LogEntry
from shared.realtime.streams import LogStreamQuery
from shared.tasks import (
    IN_FLIGHT_TASK_STATUSES,
    Task,
    TaskAttempt,
    TaskDependency,
    TaskProgressSnapshot,
    TaskResultSnapshot,
    TaskStatus,
    TaskSummary,
    is_terminal_task_status,
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
from sqlalchemy.orm import Session


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


class TaskStatusTally(BaseModel):
    """How many tasks hold one status, counted by the database."""

    status: TaskStatus
    count: int


class TaskDeploymentTally(TaskStatusTally):
    """The same tally, split by the deployment the tasks belong to.

    Null where no deployment owns the work, as the column stores it. What that
    group is called belongs to whoever renders it.
    """

    deployment_id: str | None = None


class TaskCreationSample(BaseModel):
    """When a task was created and how it ended, read without the row payload."""

    created_at: datetime
    status: TaskStatus

    @field_validator("created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc_datetime(value)


class RelatedTaskRecord[T: TaskSummary](BaseModel):
    """A task with the facts a reader needs about the resources that own it.

    Names, kinds, and versions read straight off the joined tables' own columns
    rather than out of their payload documents. Absent means the joined row is
    gone, which is why each is nullable on its own.
    """

    task: T
    app_name: str | None = None
    workload_name: str | None = None
    workload_kind: StubKind | None = None
    deployment_name: str | None = None
    deployment_version: int | None = None
    container_status: ContainerStatus | None = None


class DetailedTaskRecord(RelatedTaskRecord[Task]):
    """One task read whole, including the container record itself."""

    container: ContainerRecord | None = None


class RelatedTaskPage(BaseModel):
    data: list[RelatedTaskRecord[TaskProgressSnapshot]]
    next: str = ""


@dataclass(frozen=True, slots=True)
class LogPageCursor:
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class LogPageRecord:
    entry: LogEntry
    cursor: LogPageCursor
    workspace_id: str
    app_id: str = ""
    deployment_id: str = ""
    stub_id: str = ""
    container_id: str = ""
    machine_id: str = ""
    worker_id: str = ""


@dataclass(frozen=True, slots=True)
class LogPage:
    data: tuple[LogPageRecord, ...]
    next: LogPageCursor | None = None


DEFAULT_DURATION_SAMPLE_LIMIT = 10_000


@dataclass(slots=True)
class TaskRepository:
    session: Session

    def upsert(self, task: Task, *, workspace_id: str | None = None) -> Task:
        """System-authority write keyed by task id; ownership comes from the record."""
        owner_id = workspace_id or task.workspace_id
        if task.workspace_id is not None and owner_id != task.workspace_id:
            raise ConflictError("task ownership cannot change")
        if owner_id is not None:
            WorkspaceRepository(self.session).lock_active_owner(owner_id)
        task = Task.model_validate(dict(task) | {"workspace_id": owner_id})
        row = self.session.get(TaskTable, task.id)
        if row is None:
            row = TaskTable(id=task.id)
            self.session.add(row)
        elif row.workspace_id != owner_id:
            raise ConflictError("task ownership cannot change")
        write_task_row(row, task)
        self.session.flush()
        return task_from_table(row)

    def get(self, task_id: str, *, workspace_id: str) -> Task | None:
        if not _is_uuid_text(task_id):
            return None
        row = self.session.scalar(
            select(TaskTable).where(
                TaskTable.id == task_id,
                TaskTable.workspace_id == workspace_id,
            )
        )
        return task_from_table(row) if row is not None else None

    def get_across_workspaces(self, task_id: str) -> Task | None:
        """System lookup for scheduler/worker/runner paths acting on placed work."""
        if not _is_uuid_text(task_id):
            return None
        row = self.session.get(TaskTable, task_id)
        return task_from_table(row) if row is not None else None

    def progress_snapshot(self, task_id: str) -> TaskProgressSnapshot | None:
        if not _is_uuid_text(task_id):
            return None
        row = (
            self.session.execute(select(*_task_summary_columns()).where(TaskTable.id == task_id))
            .mappings()
            .one_or_none()
        )
        return TaskProgressSnapshot.model_validate(dict(row)) if row is not None else None

    def result_snapshot(self, task_id: str) -> TaskResultSnapshot | None:
        if not _is_uuid_text(task_id):
            return None
        row = (
            self.session.execute(
                select(TaskTable.id, TaskTable.function_result, TaskTable.error).where(
                    TaskTable.id == task_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return TaskResultSnapshot.model_validate(dict(row)) if row is not None else None

    def delete(self, task_id: str, *, workspace_id: str) -> bool:
        if not _is_uuid_text(task_id):
            return False
        return (
            self.session.scalar(
                delete(TaskTable)
                .where(
                    TaskTable.id == task_id,
                    TaskTable.workspace_id == workspace_id,
                )
                .returning(TaskTable.id)
            )
            is not None
        )

    def get_for_update_across_workspaces(self, task_id: str) -> Task | None:
        """System claim path; locks the row regardless of owning workspace."""
        statement = select(TaskTable).where(TaskTable.id == task_id).with_for_update()
        row = self.session.scalars(statement).first()
        return task_from_table(row) if row is not None else None

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
        task = task_from_table(row)
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
        claim_id: str | None = None,
    ) -> list[Task]:
        """Take up to `limit` runnable tasks for this stub, binding them to a container.

        `SKIP LOCKED` is what makes two containers asking at once safe: the loser
        steps over the row the winner is holding rather than blocking behind it or
        taking it twice. Writing `container_id` inside the same transaction *is*
        the claim — it is the field every later reader already treats as ownership,
        so a claim and a pre-assignment are indistinguishable downstream.

        Only tasks whose inputs have resolved are visible here, so a dependent
        cannot be picked up before the results it is waiting on exist.

        A `claim_id` this container already used answers with the task it took,
        still running, or with nothing once that attempt has ended. The lookup
        follows the container row lock, so a retry racing its own first request
        waits for that commit instead of missing it and taking a second task.
        """

        if limit <= 0:
            return []
        rollouts = ContainerRolloutRepository(self.session)
        admitting = rollouts.accepting_work(container_id, stub_id=stub_id)
        if claim_id is not None:
            prior = self.session.execute(
                select(TaskTable, TaskAttemptTable.status, TaskAttemptTable.attempt_number)
                .join(TaskAttemptTable, TaskAttemptTable.task_id == TaskTable.id)
                .where(
                    TaskAttemptTable.container_id == container_id,
                    TaskAttemptTable.claim_id == claim_id,
                )
            ).one_or_none()
            if prior is not None:
                row, attempt_status, attempt_number = prior
                resumable = (
                    attempt_status == TaskStatus.Running.value
                    and attempt_number == row.attempt_number
                    and row.status == TaskStatus.Running.value
                    and row.container_id == container_id
                )
                return [task_from_table(row)] if resumable else []
        if not admitting:
            return []
        rollouts.record_workload_ready(container_id, now=datetime.now(UTC))
        rows = (
            self.session.scalars(
                select(TaskTable)
                .where(TaskTable.stub_id == stub_id, _unclaimed_task())
                .order_by(TaskTable.claimable_at, TaskTable.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
            .unique()
            .all()
        )
        claimed: list[Task] = []
        for row in rows:
            task = task_from_table(row)
            task.container_id = container_id
            claimed.append(self.upsert(task))
        return claimed

    def release_claim(self, task_id: str, *, container_id: str | None) -> Task | None:
        """Give a claimed task back, so some container can take it again.

        The inverse of `claim_for_stub`, and the reason a container may be
        stopped without losing the invocation it was holding. `claimable_at`
        stays: readiness was established once and releasing does not unmake it,
        which is what lets the row go straight back to being claimable rather
        than waiting on its dependencies a second time.

        Returns `None` when the task finished or its claim moved. The expected
        container is checked under the row lock so a delayed exit cannot clear
        a replacement container's claim.
        """

        row = self.session.scalars(
            select(TaskTable).where(TaskTable.id == task_id).with_for_update()
        ).first()
        if row is None:
            return None
        task = task_from_table(row)
        if is_terminal_task_status(task.status) or task.container_id != container_id:
            return None
        task.container_id = None
        task.status = TaskStatus.Pending
        task.started_at = None
        return self.upsert(task)

    def cancel_queued_for_app(
        self,
        *,
        workspace_id: str,
        app_id: str,
        error: str,
        deployment_ids: Collection[str] | None = None,
    ) -> list[Task]:
        """Cancel queued work for the app or its selected deployments.

        Container shutdown settles running tasks. Keep terminal history unchanged,
        and select queued work by status because a retry can still name its prior
        container. Cancellation prevents another retry after deletion.
        """

        statement = (
            select(TaskTable)
            .where(
                TaskTable.app_id == app_id,
                TaskTable.workspace_id == workspace_id,
                TaskTable.status.in_(
                    [TaskStatus.Pending.value, TaskStatus.Retry.value],
                ),
            )
            .with_for_update()
        )
        if deployment_ids is not None:
            statement = statement.where(TaskTable.deployment_id.in_(deployment_ids))
        rows = self.session.scalars(statement)
        cancelled: list[Task] = []
        for row in rows:
            task = task_from_table(row)
            task.status = TaskStatus.Cancelled
            task.error = error
            task.finished_at = datetime.now(UTC)
            cancelled.append(self.upsert(task))
        return cancelled

    def containers_with_inflight_work(self, container_ids: Sequence[str]) -> set[str]:
        """Which of these containers is holding work, as one question.

        Answered with the ids alone: the caller is deciding what may be stopped,
        so it needs membership rather than the tasks themselves, and reading the
        rows back would deserialise every in-flight invocation to answer a
        yes-or-no about each container.
        """

        ids = [container_id for container_id in container_ids if container_id]
        if not ids:
            return set()
        rows = self.session.scalars(
            select(TaskTable.container_id)
            .where(
                TaskTable.container_id.in_(ids),
                TaskTable.status.in_([status.value for status in IN_FLIGHT_TASK_STATUSES]),
            )
            .distinct()
        )
        return {str(value) for value in rows if value}

    def list_inflight_for_container(self, container_id: str) -> list[Task]:
        """Work this container has claimed and not finished.

        What a stopping container is still holding. Read from the task side
        rather than the container's own `task_id`, because a pooled container is
        not started for a task and that field says nothing about what it went on
        to claim.
        """

        rows = self.session.scalars(
            select(TaskTable).where(
                TaskTable.container_id == container_id,
                TaskTable.status.in_([status.value for status in IN_FLIGHT_TASK_STATUSES]),
            )
        )
        return [task_from_table(row) for row in rows]

    def list_unclaimed_claimable(self, *, limit: int, stub_id: str | None = None) -> list[Task]:
        """Runnable work nobody has taken, oldest first, across every workspace.

        Read by the sweep that has to notice work with nowhere to run: a claim
        makes a task somebody's responsibility, and until one happens the task is
        only as alive as the container that was expected to ask for it.

        `stub_id` narrows it for a caller provisioning one stub. Reading the
        oldest row overall and discarding it when it belongs elsewhere answers a
        different question — whether this stub owns the platform's oldest work —
        and a stub whose backlog arrived second would never be provisioned.
        """

        rows = self.session.scalars(
            select(TaskTable)
            .where(
                TaskTable.status == TaskStatus.Pending.value,
                TaskTable.container_id.is_(None),
                TaskTable.claimable_at.is_not(None),
                *([TaskTable.stub_id == stub_id] if stub_id is not None else []),
            )
            .order_by(TaskTable.claimable_at, TaskTable.id)
            .limit(limit)
        )
        return [task_from_table(row) for row in rows]

    def list_unclaimed_claimable_for_update(
        self,
        *,
        stub_id: str,
        limit: int,
    ) -> list[Task]:
        """Lock runnable work for one stub without racing a container claim."""

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
        return [task_from_table(row) for row in rows]

    def count_unclaimed_by_stub(self, stub_ids: Sequence[str]) -> dict[str, int]:
        """Runnable, unclaimed work for several stubs in one grouped query."""
        return self._count_by_stub(stub_ids, _unclaimed_task())

    def count_demand_by_stub(self, stub_ids: Sequence[str]) -> dict[str, int]:
        """Work that needs a container slot: runnable and unclaimed, or claimed and in flight.

        Tasks still waiting on their dependencies are left out, since no
        container can run them yet.
        """
        return self._count_by_stub(
            stub_ids,
            or_(
                _unclaimed_task(),
                TaskTable.container_id.is_not(None)
                & TaskTable.status.in_([status.value for status in IN_FLIGHT_TASK_STATUSES]),
            ),
        )

    def _count_by_stub(
        self, stub_ids: Sequence[str], condition: ColumnElement[bool]
    ) -> dict[str, int]:
        ids = tuple(dict.fromkeys(stub_id for stub_id in stub_ids if stub_id))
        counts = dict.fromkeys(ids, 0)
        if not ids:
            return counts
        rows = self.session.execute(
            select(TaskTable.stub_id, func.count(TaskTable.id))
            .where(TaskTable.stub_id.in_(ids), condition)
            .group_by(TaskTable.stub_id)
        )
        for stub_id, count in rows:
            if stub_id is not None:
                counts[str(stub_id)] = int(count)
        return counts

    def due_retry_tasks(self, *, now: datetime, limit: int) -> list[Task]:
        """Due retry work in the order the retry scheduler consumes it."""
        if limit <= 0:
            return []
        due_at = case(
            (TaskTable.next_retry_at.is_(None), TaskTable.created_at),
            else_=TaskTable.next_retry_at,
        )
        rows = self.session.scalars(
            select(TaskTable)
            .where(
                TaskTable.status == TaskStatus.Retry.value,
                or_(TaskTable.next_retry_at.is_(None), TaskTable.next_retry_at <= now),
            )
            .order_by(due_at, TaskTable.created_at, TaskTable.id)
            .limit(limit)
        )
        return [task_from_table(row) for row in rows]

    def count_inflight_for_stub(self, stub_id: str) -> int:
        """Everything for this stub that has not finished, claimed or not.

        The population a backpressure limit is about: work the caller is still
        owed an answer for. Counted rather than derived from the claimed and
        unclaimed counts separately, so a task moving between those two states
        while both were read cannot be missed by one and double-counted by the
        other.
        """

        return int(
            self.session.scalar(
                select(func.count(TaskTable.id)).where(
                    TaskTable.stub_id == stub_id,
                    TaskTable.status.in_([status.value for status in IN_FLIGHT_TASK_STATUSES]),
                )
            )
            or 0
        )

    def list(
        self,
        *,
        workspace_id: str,
        status: str | None = None,
    ) -> list[Task]:
        statement = select(TaskTable).where(TaskTable.workspace_id == workspace_id)
        if status is not None:
            statement = statement.where(TaskTable.status == status)
        return [
            task_from_table(row)
            for row in self.session.scalars(
                statement.order_by(TaskTable.created_at.desc(), TaskTable.id)
            )
        ]

    def list_across_workspaces(self, *, status: str | None = None) -> list[Task]:
        """System listing for reconcilers and schedulers over every workspace."""
        statement = select(TaskTable)
        if status is not None:
            statement = statement.where(TaskTable.status == status)
        return [
            task_from_table(row)
            for row in self.session.scalars(
                statement.order_by(TaskTable.created_at.desc(), TaskTable.id)
            )
        ]

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
        limit: int = 50,
        offset: int = 0,
    ) -> RelatedTaskPage:
        """Read a filtered task page with its owning resources named in SQL."""
        statement = (
            _related_task_statement()
            .with_only_columns(
                *_task_summary_columns(),
                AppTable.name.label("app_name"),
                StubTable.name.label("workload_name"),
                StubTable.type.label("workload_kind"),
                DeploymentTable.name.label("deployment_name"),
                DeploymentTable.version.label("deployment_version"),
                ContainerTable.status.label("container_status"),
                maintain_column_froms=True,
            )
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
        statement = (
            statement.order_by(TaskTable.created_at.desc(), TaskTable.id.desc())
            .offset(max(offset, 0))
            .limit(max(limit, 1) + 1)
        )
        rows = list(self.session.execute(statement).mappings())
        page_rows = rows[: max(limit, 1)]
        return RelatedTaskPage(
            data=[
                RelatedTaskRecord(
                    task=TaskProgressSnapshot.model_validate(
                        {key: row[key] for key in TaskProgressSnapshot.model_fields}
                    ),
                    app_name=row.app_name,
                    workload_name=row.workload_name,
                    workload_kind=StubKind(row.workload_kind)
                    if row.workload_kind is not None
                    else None,
                    deployment_name=row.deployment_name,
                    deployment_version=row.deployment_version,
                    container_status=(
                        ContainerStatus(row.container_status)
                        if row.container_status is not None
                        else None
                    ),
                )
                for row in page_rows
            ],
            next=str(max(offset, 0) + len(page_rows)) if len(rows) > len(page_rows) else "",
        )

    def get_with_related(self, task_id: str, *, workspace_id: str) -> DetailedTaskRecord | None:
        """Read one task with its owning resources and the container it ran in."""
        if not _is_uuid_text(task_id):
            return None
        statement = (
            _related_task_statement()
            .add_columns(ContainerTable)
            .where(TaskTable.workspace_id == workspace_id, TaskTable.id == task_id)
        )
        row = self.session.execute(statement).tuples().first()
        if row is None:
            return None
        (
            task_row,
            app_name,
            stub_name,
            stub_type,
            deployment_name,
            deployment_version,
            container_status,
            container_row,
        ) = row
        return DetailedTaskRecord(
            task=task_from_table(task_row),
            app_name=app_name,
            workload_name=stub_name,
            workload_kind=StubKind(stub_type) if stub_type is not None else None,
            deployment_name=deployment_name,
            deployment_version=deployment_version,
            container_status=(
                ContainerStatus(container_status) if container_status is not None else None
            ),
            container=(container_from_row(container_row) if container_row else None),
        )

    def ids_for_container(self, container_id: str) -> list[str]:
        """Ids of tasks bound to a container via the indexed column or kwargs."""
        conditions = [TaskTable.input_container_id == container_id]
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
        app_id: str | None = None,
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
        if app_id is not None:
            statement = statement.where(TaskTable.app_id == app_id)
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

    def status_tallies(
        self,
        *,
        workspace_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        app_id: str | None = None,
        stub_id: str | None = None,
    ) -> list[TaskStatusTally]:
        """Counts per status, answered by the database rather than by rows.

        Counting in Python meant reading every task the filter accepted, and a
        task row carries its `args`, `kwargs` and `result` inline. A summary of a
        few integers pulled the whole table across the wire, which is what made a
        dashboard left open overnight the most expensive thing in the account.
        """
        statement = select(TaskTable.status, func.count(TaskTable.id).label("count")).where(
            TaskTable.workspace_id == workspace_id
        )
        statement = _narrowed_tasks(statement, start=start, end=end, app_id=app_id, stub_id=stub_id)
        statement = statement.group_by(TaskTable.status)
        return [
            TaskStatusTally.model_validate(row)
            for row in self.session.execute(statement).mappings()
        ]

    def status_tallies_by_deployment(self, *, workspace_id: str) -> list[TaskDeploymentTally]:
        """The same counts, split by deployment.

        Grouped on the column as it is stored, null included. Work no deployment
        owns is a real group here and the name it is reported under is the
        caller's contract rather than the database's, so folding it in would mean
        coalescing a UUID column with a text stand-in: PostgreSQL refuses that
        outright while SQLite accepts it, which is a statement that passes on the
        cheap backend and fails on every request against the real one.
        """
        statement = (
            select(
                TaskTable.deployment_id,
                TaskTable.status,
                func.count(TaskTable.id).label("count"),
            )
            .where(TaskTable.workspace_id == workspace_id)
            .group_by(TaskTable.deployment_id, TaskTable.status)
        )
        return [
            TaskDeploymentTally.model_validate(row)
            for row in self.session.execute(statement).mappings()
        ]

    def creation_samples(
        self,
        *,
        workspace_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        app_id: str | None = None,
        stub_id: str | None = None,
        limit: int = DEFAULT_DURATION_SAMPLE_LIMIT,
    ) -> list[TaskCreationSample]:
        """Creation times and statuses for bucketing, two columns per task.

        The bucket width is the caller's, so the grouping stays out of SQL: the
        expression that divides a timestamp into windows is written differently
        by every backend, and this repository is read by both. Two scalar columns
        per row is already three orders of magnitude below reading the rows.
        """
        statement = select(TaskTable.created_at, TaskTable.status).where(
            TaskTable.workspace_id == workspace_id
        )
        statement = _narrowed_tasks(statement, start=start, end=end, app_id=app_id, stub_id=stub_id)
        statement = statement.order_by(TaskTable.created_at.desc(), TaskTable.id.desc()).limit(
            max(limit, 1)
        )
        rows = list(self.session.execute(statement).mappings())
        rows.reverse()
        return [TaskCreationSample.model_validate(row) for row in rows]

    def existing_ids(self, *, workspace_id: str, task_ids: Sequence[str]) -> set[str]:
        """Which of these ids the workspace holds, asked as one question.

        Anything that is not a UUID cannot name a row, so it is dropped here
        rather than handed to the database, which would refuse the whole
        statement over one malformed id a caller supplied.
        """
        candidates: list[str] = []
        for task_id in task_ids:
            try:
                candidates.append(str(UUID(task_id)))
            except (AttributeError, TypeError, ValueError):
                continue
        if not candidates:
            return set()
        rows = self.session.scalars(
            select(TaskTable.id).where(
                TaskTable.workspace_id == workspace_id,
                TaskTable.id.in_(candidates),
            )
        )
        return {str(value) for value in rows}


def _narrowed_tasks[StatementT: Select[Any]](
    statement: StatementT,
    *,
    start: datetime | None,
    end: datetime | None,
    app_id: str | None,
    stub_id: str | None,
) -> StatementT:
    """The window and scope every task aggregate shares, applied in SQL.

    One place, because the aggregates have to agree about what they counted: a
    summary windowed here and a chart windowed in the caller would disagree at
    the edges and the difference would read as lost work.
    """
    if start is not None:
        statement = statement.where(TaskTable.created_at >= start)
    if end is not None:
        statement = statement.where(TaskTable.created_at <= end)
    if app_id is not None:
        statement = statement.where(TaskTable.app_id == app_id)
    if stub_id is not None:
        statement = statement.where(TaskTable.stub_id == stub_id)
    return statement


def _task_summary_columns():
    return (
        TaskTable.id,
        TaskTable.name,
        TaskTable.status,
        TaskTable.workspace_id,
        TaskTable.app_id,
        TaskTable.stub_id,
        TaskTable.deployment_id,
        TaskTable.container_id,
        TaskTable.parent_task_id,
        TaskTable.root_task_id,
        TaskTable.handler,
        TaskTable.attempt_number,
        TaskTable.max_attempts,
        TaskTable.next_retry_at,
        TaskTable.exit_code,
        TaskTable.created_at,
        TaskTable.started_at,
        TaskTable.finished_at,
        func.coalesce(func.jsonb_typeof(TaskTable.invocation) == "object", False).label(
            "is_function"
        ),
        TaskTable.claimable_at,
    )


def _related_task_statement() -> Select[tuple[TaskTable, str, str, str, str, int, str]]:
    """Tasks joined to the columns that name their app, workload, and container.

    Columns rather than the joined rows' payload documents. Every fact a reader
    of a task needs about the resources around it is indexed beside the row
    already, and deserializing four documents per row to reach four fields is
    what makes a page of tasks expensive. The container contributes only its
    status, which is what decides whether a shell can still attach.
    """

    return (
        select(
            TaskTable,
            AppTable.name,
            StubTable.name,
            StubTable.type,
            DeploymentTable.name,
            DeploymentTable.version,
            ContainerTable.status,
        )
        .select_from(TaskTable)
        .outerjoin(AppTable, AppTable.id == TaskTable.app_id)
        .outerjoin(StubTable, StubTable.id == TaskTable.stub_id)
        .outerjoin(DeploymentTable, DeploymentTable.id == TaskTable.deployment_id)
        .outerjoin(ContainerTable, ContainerTable.id == TaskTable.container_id)
    )


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

    def expired_function_attempts(self, *, now: datetime, limit: int) -> list[TaskAttempt]:
        timeout = func.coalesce(
            StubTable.runtime_timeout_seconds,
            DEFAULT_FUNCTION_TIMEOUT_SECONDS,
        )
        statement = (
            select(TaskAttemptTable)
            .join(TaskTable, TaskTable.id == TaskAttemptTable.task_id)
            .join(StubTable, StubTable.id == TaskTable.stub_id)
            .where(
                StubTable.type == StubKind.Function.value,
                TaskTable.status == TaskStatus.Running.value,
                TaskAttemptTable.status == TaskStatus.Running.value,
                TaskAttemptTable.attempt_number == TaskTable.attempt_number,
                TaskAttemptTable.container_id == TaskTable.container_id,
                timeout > 0,
                func.extract("epoch", now - TaskAttemptTable.started_at) >= timeout,
            )
            .order_by(TaskAttemptTable.started_at, TaskAttemptTable.id)
            .limit(limit)
        )
        return [task_attempt_from_table(row) for row in self.session.scalars(statement)]

    def containers_with_timed_out_attempts(self, *, limit: int) -> list[str]:
        statement = (
            select(ContainerTable.id)
            .join(TaskAttemptTable, TaskAttemptTable.container_id == ContainerTable.id)
            .join(StubTable, StubTable.id == ContainerTable.stub_id)
            .where(
                StubTable.type == StubKind.Function.value,
                TaskAttemptTable.status == TaskStatus.Timeout.value,
                ContainerTable.status.in_(
                    [ContainerStatus.Pending.value, ContainerStatus.Running.value]
                ),
            )
            .distinct()
            .order_by(ContainerTable.id)
            .limit(limit)
        )
        return [str(identifier) for identifier in self.session.scalars(statement)]

    def latest_finished_at_for_container(self, container_id: str) -> datetime | None:
        return self.session.scalar(
            select(func.max(TaskAttemptTable.finished_at)).where(
                TaskAttemptTable.container_id == container_id
            )
        )

    def create(self, attempt: TaskAttempt, *, claim_id: str | None = None) -> TaskAttempt:
        """System-authority write; ownership comes from the attempt record."""
        if attempt.workspace_id is not None:
            WorkspaceRepository(self.session).lock_active_owner(attempt.workspace_id)
        row = TaskAttemptTable(id=attempt.id, claim_id=claim_id)
        write_task_attempt_row(row, attempt)
        self.session.add(row)
        self.session.flush()
        return task_attempt_from_table(row)

    def upsert(self, attempt: TaskAttempt, *, claim_id: str | None = None) -> TaskAttempt:
        """System-authority write keyed by attempt id; ownership comes from the record."""
        if attempt.workspace_id is not None:
            WorkspaceRepository(self.session).lock_active_owner(attempt.workspace_id)
        row = self.session.get(TaskAttemptTable, attempt.id)
        if row is None:
            row = TaskAttemptTable(id=attempt.id)
            self.session.add(row)
        elif row.workspace_id != attempt.workspace_id or row.task_id != attempt.task_id:
            raise ConflictError("task attempt ownership cannot change")
        write_task_attempt_row(row, attempt)
        if claim_id is not None:
            row.claim_id = claim_id
        self.session.flush()
        return task_attempt_from_table(row)

    def list_for_task(self, task_id: str) -> list[TaskAttempt]:
        statement = (
            select(TaskAttemptTable)
            .where(TaskAttemptTable.task_id == task_id)
            .order_by(TaskAttemptTable.attempt_number.asc(), TaskAttemptTable.created_at.asc())
        )
        return [task_attempt_from_table(row) for row in self.session.scalars(statement)]

    def latest_task_id_for_container(self, container_id: str) -> str:
        """Which task this container most recently attempted.

        Survives the task being settled, unlike the claim: finishing a task
        clears `container_id`, so a second settlement of the same container has
        nothing left on the task to find it by. The attempt row is the durable
        record that this container ran this work, and it is what makes settling
        twice resolve the same task and report the same no-op.
        """

        statement = (
            select(TaskAttemptTable)
            .where(TaskAttemptTable.container_id == container_id)
            .order_by(TaskAttemptTable.created_at.desc(), TaskAttemptTable.attempt_number.desc())
            .limit(1)
        )
        row = self.session.scalars(statement).first()
        return str(row.task_id) if row is not None else ""

    def latest_for_task(self, task_id: str) -> TaskAttempt | None:
        statement = (
            select(TaskAttemptTable)
            .where(TaskAttemptTable.task_id == task_id)
            .order_by(TaskAttemptTable.attempt_number.desc(), TaskAttemptTable.created_at.desc())
            .limit(1)
        )
        row = self.session.scalars(statement).first()
        return task_attempt_from_table(row) if row is not None else None

    def latest_finished_for_tasks(self, task_ids: Sequence[str]) -> dict[str, datetime]:
        if not task_ids:
            return {}
        statement = (
            select(TaskAttemptTable.task_id, TaskAttemptTable.finished_at)
            .where(TaskAttemptTable.task_id.in_(task_ids))
            .distinct(TaskAttemptTable.task_id)
            .order_by(
                TaskAttemptTable.task_id,
                TaskAttemptTable.attempt_number.desc(),
                TaskAttemptTable.created_at.desc(),
            )
        )
        return {
            str(row.task_id): _utc_datetime(row.finished_at)
            for row in self.session.execute(statement)
            if row.finished_at is not None
        }


@dataclass(slots=True)
class TaskDependencyRepository:
    session: Session

    def create(self, dependency: TaskDependency) -> TaskDependency:
        """System-authority write; ownership comes from the dependency record."""
        if dependency.workspace_id is not None:
            WorkspaceRepository(self.session).lock_active_owner(dependency.workspace_id)
        row = TaskDependencyTable(
            id=dependency.id or str(uuid4()),
            workspace_id=dependency.workspace_id,
            task_id=dependency.task_id,
            upstream_task_id=dependency.upstream_task_id,
            parent_task_id=dependency.parent_task_id,
            root_task_id=dependency.root_task_id,
            edge_type=dependency.edge_type,
            created_at=dependency.created_at,
        )
        self.session.add(row)
        self.session.flush()
        return task_dependency_from_table(row)

    def list_for_task(self, task_id: str) -> list[TaskDependency]:
        statement = (
            select(TaskDependencyTable)
            .where(TaskDependencyTable.task_id == task_id)
            .order_by(TaskDependencyTable.created_at.asc(), TaskDependencyTable.id.asc())
        )
        return [task_dependency_from_table(row) for row in self.session.scalars(statement)]

    def list_for_upstream(self, upstream_task_id: str) -> list[TaskDependency]:
        statement = (
            select(TaskDependencyTable)
            .where(TaskDependencyTable.upstream_task_id == upstream_task_id)
            .order_by(TaskDependencyTable.created_at.asc(), TaskDependencyTable.id.asc())
        )
        return [task_dependency_from_table(row) for row in self.session.scalars(statement)]

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
        return [task_dependency_from_table(row) for row in self.session.scalars(statement)]


@dataclass(slots=True)
class LogRepository:
    session: Session

    def prune(
        self,
        *,
        cutoffs: Mapping[str, datetime],
        default_cutoff: datetime,
        complimentary_cutoff: datetime,
        limit: int,
    ) -> int:
        cutoff = case(
            (BillingAccountTable.complimentary_since.is_not(None), complimentary_cutoff),
            else_=case(dict(cutoffs), value=BillingAccountTable.plan, else_=default_cutoff),
        )
        expired = (
            select(LogTable.id)
            .outerjoin(
                WorkspaceMemberTable,
                and_(
                    WorkspaceMemberTable.workspace_id == LogTable.workspace_id,
                    WorkspaceMemberTable.role == "owner",
                ),
            )
            .outerjoin(
                BillingAccountTable, BillingAccountTable.user_id == WorkspaceMemberTable.user_id
            )
            .where(
                LogTable.created_at < max(default_cutoff, complimentary_cutoff, *cutoffs.values()),
                LogTable.created_at < cutoff,
            )
            .order_by(LogTable.created_at, LogTable.id)
            .limit(limit)
            .with_for_update(of=LogTable, skip_locked=True)
        )
        return len(
            list(
                self.session.scalars(
                    delete(LogTable).where(LogTable.id.in_(expired)).returning(LogTable.id)
                )
            )
        )

    def append(self, entry: LogEntry, *, workspace_id: str | None = None) -> LogEntry:
        """System-authority write; runner/worker logs may be cluster-level."""
        if workspace_id is not None:
            WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        row = self.session.get(LogTable, entry.id)
        if row is None:
            row = LogTable(id=entry.id, workspace_id=workspace_id)
            self.session.add(row)
        elif row.workspace_id != workspace_id:
            raise ConflictError("log ownership cannot change")
        write_log_row(row, entry)
        self.session.flush()
        self.session.refresh(row)
        return log_entry_from_table(row)

    def append_batch(self, entries: Sequence[LogEntry], *, workspace_id: str) -> None:
        if not entries:
            return
        self.session.execute(
            postgresql_insert(LogTable)
            .values(
                [
                    {
                        **entry.model_dump(),
                        "workspace_id": workspace_id,
                    }
                    for entry in entries
                ]
            )
            .on_conflict_do_nothing(index_elements=[LogTable.id])
        )

    def page(
        self,
        query: LogStreamQuery,
        *,
        workspace_id: str,
        limit: int,
        cursor: LogPageCursor | None = None,
    ) -> LogPage:
        """Read the latest matching logs, then page toward older history."""
        return self._page(
            query,
            workspace_id=workspace_id,
            limit=limit,
            before=cursor,
        )

    def page_after(
        self,
        query: LogStreamQuery,
        *,
        workspace_id: str,
        limit: int,
        cursor: LogPageCursor | None = None,
    ) -> LogPage:
        """Read matching logs after a durable follow cursor."""
        return self._page(
            query,
            workspace_id=workspace_id,
            limit=limit,
            after=cursor,
            descending=False,
        )

    def _page(
        self,
        query: LogStreamQuery,
        *,
        workspace_id: str,
        limit: int,
        before: LogPageCursor | None = None,
        after: LogPageCursor | None = None,
        descending: bool = True,
    ) -> LogPage:
        if not workspace_id:
            raise ValueError("log queries require workspace_id")
        page_limit = min(max(limit, 1), 1_000)
        identifiers = (
            query.task_id,
            query.stub_id,
            query.app_id,
            query.deployment_id,
            query.container_id,
        )
        if any(identifier and not _is_uuid_text(identifier) for identifier in identifiers):
            return LogPage(data=())
        statement = select(LogTable).where(LogTable.workspace_id == workspace_id)
        if query.task_id:
            statement = statement.where(LogTable.task_id == query.task_id)
        if query.stub_id:
            statement = statement.where(LogTable.stub_id == query.stub_id)
        if query.app_id:
            statement = statement.where(LogTable.app_id == query.app_id)
        if query.deployment_id:
            statement = statement.where(LogTable.deployment_id == query.deployment_id)
        if query.container_id:
            statement = statement.where(LogTable.container_id == query.container_id)
        if query.machine_id:
            statement = statement.where(LogTable.machine_id == query.machine_id)
        if query.worker_id:
            statement = statement.where(LogTable.worker_id == query.worker_id)
        if query.query:
            statement = statement.where(
                func.lower(LogTable.message).contains(query.query.lower(), autoescape=True)
            )
        if query.start_time is not None:
            statement = statement.where(LogTable.created_at >= query.start_time)
        if query.end_time is not None:
            statement = statement.where(LogTable.created_at < query.end_time)
        if before is not None:
            statement = statement.where(
                or_(
                    LogTable.created_at < before.created_at,
                    and_(LogTable.created_at == before.created_at, LogTable.id < before.id),
                )
            )
        if after is not None:
            statement = statement.where(
                or_(
                    LogTable.created_at > after.created_at,
                    and_(LogTable.created_at == after.created_at, LogTable.id > after.id),
                )
            )
        ordering = (
            (LogTable.created_at.desc(), LogTable.id.desc())
            if descending
            else (LogTable.created_at.asc(), LogTable.id.asc())
        )
        rows = list(self.session.scalars(statement.order_by(*ordering).limit(page_limit + 1)))
        page_rows = rows[:page_limit]
        next_cursor = None
        if len(rows) > page_limit and page_rows:
            boundary = page_rows[-1]
            next_cursor = LogPageCursor(
                created_at=_utc_datetime(boundary.created_at),
                id=str(boundary.id),
            )
        records = tuple(self._record_from_row(row) for row in page_rows)
        if descending:
            records = tuple(reversed(records))
        return LogPage(data=records, next=next_cursor)

    @staticmethod
    def _record_from_row(log: LogTable) -> LogPageRecord:
        return LogPageRecord(
            entry=log_entry_from_table(log),
            cursor=LogPageCursor(
                created_at=_utc_datetime(log.created_at),
                id=str(log.id),
            ),
            workspace_id=str(log.workspace_id),
            app_id=log.app_id or "",
            deployment_id=log.deployment_id or "",
            stub_id=log.stub_id or "",
            container_id=log.container_id or "",
            machine_id=log.machine_id or "",
            worker_id=log.worker_id or "",
        )


@dataclass(slots=True)
class EventRepository:
    session: Session

    def append(self, event: Event, *, workspace_id: str | None = None) -> Event:
        """System-authority write; cluster-level events carry no workspace."""
        if workspace_id is not None:
            WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        row = self.session.get(EventTable, event.id)
        if row is None:
            row = EventTable(id=event.id, workspace_id=workspace_id)
            self.session.add(row)
        elif row.workspace_id != workspace_id:
            raise ConflictError("event ownership cannot change")
        write_event_row(row, event)
        self.session.flush()
        return event_from_table(row)

    def list(
        self,
        *,
        workspace_id: str,
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
        return [event_from_table(row) for row in self.session.scalars(statement)]

    def _count(
        self,
        *,
        workspace_id: str | None,
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
        resource_type: str | None,
        resource_id: str | None,
        actions: Sequence[str] | None,
        since: datetime | None,
        until: datetime | None,
    ) -> TSelect:
        if workspace_id is not None:
            statement = statement.where(EventTable.workspace_id == workspace_id)
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
            EventTable.container_id == container_id,
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
                merged[row.id] = (row.created_at, row.id, event_from_table(row))
        ranked = sorted(merged.values(), key=lambda item: item[1])
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [event for _, _, event in ranked]


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
        insert = postgresql_insert(PodUrlTable).values(**values)
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


@dataclass(slots=True)
class CronJobRunRepository:
    session: Session

    def append(self, run: CronJobRun) -> CronJobRun:
        WorkspaceRepository(self.session).lock_active_owner(run.workspace_id)
        row = CronJobRunTable(
            id=run.id,
            workspace_id=run.workspace_id,
            cron_job=run.cron_job,
            enqueued=run.enqueued,
            task_id=run.task_id,
            reason=run.reason,
            created_at=run.created_at,
        )
        self.session.add(row)
        self.session.flush()
        return cron_job_run_from_table(row)

    def page(
        self,
        *,
        workspace_id: str,
        cursor: CronJobRunCursor | None,
        limit: int,
    ) -> CronJobRunPage:
        """Read one descending workspace page without offset drift."""
        page_limit = max(limit, 1)
        statement = select(CronJobRunTable).where(CronJobRunTable.workspace_id == workspace_id)
        if cursor is not None:
            statement = statement.where(
                or_(
                    CronJobRunTable.created_at < cursor.created_at,
                    and_(
                        CronJobRunTable.created_at == cursor.created_at,
                        CronJobRunTable.id < cursor.id,
                    ),
                )
            )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    CronJobRunTable.created_at.desc(),
                    CronJobRunTable.id.desc(),
                ).limit(page_limit + 1)
            )
        )
        page_rows = rows[:page_limit]
        next_cursor = None
        if len(rows) > page_limit and page_rows:
            last = page_rows[-1]
            next_cursor = CronJobRunCursor(
                created_at=_utc_datetime(last.created_at),
                id=str(last.id),
            )
        return CronJobRunPage(
            data=tuple(cron_job_run_from_table(row) for row in page_rows),
            next=next_cursor,
        )


@dataclass(frozen=True, slots=True)
class CronJobRunCursor:
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class CronJobRunPage:
    data: tuple[CronJobRun, ...]
    next: CronJobRunCursor | None = None


def _unclaimed_task() -> ColumnElement[bool]:
    return (
        (TaskTable.status == TaskStatus.Pending.value)
        & TaskTable.container_id.is_(None)
        & TaskTable.claimable_at.is_not(None)
    )
