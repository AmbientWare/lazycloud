from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from control.service import ControlServices
from database.context import ServiceContext
from database.records.apps import StubKind, StubRecord
from database.repositories.execution import DetailedTaskRecord, RelatedTaskRecord, TaskRepository
from execution.functions.service import FunctionControlService
from execution.tasks import TaskService
from shared.containers import ContainerStatus
from shared.errors import InvalidInputError, NotFoundError
from shared.http.tasks import (
    TaskActionCapabilitiesResponse,
    TaskAppReferenceResponse,
    TaskDeploymentReferenceResponse,
    TaskWorkloadReferenceResponse,
)
from shared.tasks import Task, TaskProgressSnapshot, TaskStatus, is_terminal_task_status
from shared.timestamps import utc_now

from operations.management import (
    DEFAULT_TASK_WINDOW_BUCKETS,
    CursorPage,
    TaskCountByDeployment,
    TaskDetailView,
    TaskMetricsSummary,
    TaskStopResult,
    TaskTimeWindowBucket,
    TaskView,
    _bucket_start,
    _parse_cursor,
    _percentile,
    _sample_runtime_ms,
    _sample_startup_ms,
)


def _task_actions(
    record: RelatedTaskRecord[Task] | RelatedTaskRecord[TaskProgressSnapshot],
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


def _task_app(
    record: RelatedTaskRecord[Task] | RelatedTaskRecord[TaskProgressSnapshot],
) -> TaskAppReferenceResponse | None:
    if record.app_name is None:
        return None
    return TaskAppReferenceResponse(name=record.app_name)


def _task_workload(
    record: RelatedTaskRecord[Task] | RelatedTaskRecord[TaskProgressSnapshot],
) -> TaskWorkloadReferenceResponse | None:
    if record.workload_name is None or record.workload_kind is None:
        return None
    return TaskWorkloadReferenceResponse(name=record.workload_name, kind=record.workload_kind)


def _task_deployment(
    record: RelatedTaskRecord[Task] | RelatedTaskRecord[TaskProgressSnapshot],
) -> TaskDeploymentReferenceResponse | None:
    if record.deployment_name is None or record.deployment_version is None:
        return None
    return TaskDeploymentReferenceResponse(
        name=record.deployment_name,
        version=record.deployment_version,
    )


def _task_view(
    record: RelatedTaskRecord[TaskProgressSnapshot], *, can_write: bool
) -> TaskView[TaskProgressSnapshot]:
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


@dataclass(frozen=True, slots=True)
class TaskManagementService:
    context: ServiceContext
    tasks: TaskService
    functions: FunctionControlService

    @property
    def control_plane(self) -> ControlServices:
        return ControlServices.create(self.context)

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
    ) -> CursorPage[TaskView[TaskProgressSnapshot]]:
        workspace_record = self.control_plane.workspaces.get_workspace(workspace)
        offset = _parse_cursor(cursor)
        with self.context.database.session() as session:
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
        progress = self.tasks.progress.read([item.task for item in page.data])
        return CursorPage(
            data=tuple(
                _task_view(item, can_write=can_write).model_copy(
                    update={"pending_progress": progress[item.task.id]}
                )
                for item in page.data
            ),
            next=page.next,
        )

    def task_detail(
        self,
        workspace: str,
        task_id: str,
        *,
        can_write: bool = False,
    ) -> TaskDetailView:
        workspace_record = self.control_plane.workspaces.get_workspace(workspace)
        with self.context.database.session() as session:
            related = TaskRepository(session).get_with_related(
                task_id,
                workspace_id=workspace_record.id,
            )
        if related is None:
            msg = f"task not found in workspace: {task_id}"
            raise NotFoundError(msg)
        progress = self.tasks.progress.read([related.task])
        return _task_detail_view(related, can_write=can_write).model_copy(
            update={"pending_progress": progress[related.task.id]}
        )

    def workspace_task(self, workspace: str, task_id: str) -> Task:
        return self.task_detail(workspace, task_id).task

    def task_counts_by_deployment(self, workspace: str) -> tuple[TaskCountByDeployment, ...]:
        workspace_record = self.control_plane.workspaces.get_workspace(workspace)
        with self.context.database.session() as session:
            tallies = TaskRepository(session).status_tallies_by_deployment(
                workspace_id=workspace_record.id
            )
        counts: dict[str, Counter[TaskStatus]] = defaultdict(Counter)
        for tally in tallies:
            counts[tally.deployment_id or ""][tally.status] += tally.count
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
        """Bound default chart reads to the most recent 48 buckets."""
        if window_seconds <= 0:
            msg = "window_seconds must be greater than zero"
            raise InvalidInputError(msg)
        end = ended_at or utc_now()
        start = started_at or end - timedelta(seconds=window_seconds * DEFAULT_TASK_WINDOW_BUCKETS)
        workspace_record = self.control_plane.workspaces.get_workspace(workspace)
        with self.context.database.session() as session:
            samples = TaskRepository(session).creation_samples(
                workspace_id=workspace_record.id,
                start=start,
                end=end,
                app_id=app_id,
                stub_id=stub_id,
            )
        buckets: dict[datetime, Counter[TaskStatus]] = defaultdict(Counter)
        for sample in samples:
            bucket = _bucket_start(sample.created_at, window_seconds)
            buckets[bucket][sample.status] += 1
        return tuple(
            TaskTimeWindowBucket(
                timestamp=timestamp,
                count=sum(counter.values()),
                status_counts=dict(counter),
            )
            for timestamp, counter in sorted(buckets.items())
        )

    def stop_tasks(self, workspace: str, task_ids: list[str]) -> TaskStopResult:
        workspace_record = self.control_plane.workspaces.get_workspace(workspace)
        with self.context.database.session() as session:
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
            task = self.tasks.get(task_id)
            if is_terminal_task_status(task.status):
                skipped.append(task_id)
                continue
            self._cancel_task(task)
            stopped.append(task_id)
        return TaskStopResult(stopped=tuple(stopped), skipped=tuple(skipped))

    def _cancel_task(self, task: Task) -> None:
        """Let the workload owner cancel execution and settle its dependents."""

        stub = self._stub_for_task(task)
        if stub is not None and stub.kind is StubKind.Function:
            self.functions.cancel_task(task.id)
            return
        self.tasks.cancel(task.id)

    def _stub_for_task(self, task: Task) -> StubRecord | None:
        if not task.stub_id:
            return None
        try:
            return self.control_plane.stubs.get_stub(task.stub_id)
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
        workspace_record = self.control_plane.workspaces.get_workspace(workspace)
        with self.context.database.session() as session:
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
