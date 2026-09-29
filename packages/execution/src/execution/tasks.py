from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS
from database.repositories.apps import StubRepository
from database.repositories.cleanup import CleanupRepository
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.execution import (
    LogPage,
    LogPageCursor,
    LogRepository,
    TaskAttemptRepository,
    TaskClaimCollisionError,
    TaskRepository,
)
from database.repositories.orchestration import ContainerRepository
from database.types import DatabaseSession
from foundation.ids import optional_uuid, required_uuid
from observability.events import EventService
from observability.log_retention import LogRetentionService
from observability.stream_state import AsyncRedisEventStreamRepository, RedisEventStreamRepository
from observability.workspace_changes import AsyncWorkspaceChangeService, WorkspaceChangePublisher
from pydantic import JsonValue
from shared.containers import TERMINAL_CONTAINER_STATUSES, ContainerStatus
from shared.deployments import StubKind
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.events import Event, EventLevel
from shared.function_payloads import FunctionInvocationPayload, FunctionResultPayload
from shared.http.execution_entry import ExecutionEntryEvidence
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.logs import LogEntry
from shared.realtime.contracts import EventDataInput, EventRecordType
from shared.realtime.streams import LogStreamQuery
from shared.tasks import (
    RetryDecision,
    RetryPolicy,
    Task,
    TaskAttempt,
    TaskProgressSnapshot,
    TaskStatus,
    is_terminal_task_status,
    normalize_retry_policy,
    plan_retry,
)
from shared.timestamps import utc_now

from database import AsyncDatabaseClient
from execution.callbacks import TaskCallbackDispatcher, TaskCallbackService
from execution.context import ExecutionContext
from execution.task_progress import TaskProgressService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskClaimOutcome:
    task: Task | None
    queue_checked: bool


@dataclass(frozen=True, slots=True)
class TaskFinishOutcome:
    task: Task
    retry_decision: RetryDecision
    state_changed: bool
    claim_acknowledged: bool
    callback_url: str | None


@dataclass(frozen=True, slots=True)
class TaskCancellationOutcome:
    task: Task
    state_changed: bool


@dataclass(frozen=True, slots=True)
class _TaskStartPersistence:
    task: Task
    attempt_started: bool
    attempt_container_id: str | None


@dataclass(frozen=True, slots=True)
class _TaskStartDecision:
    persisted: _TaskStartPersistence
    attempt: TaskAttempt | None
    record_running: bool


@dataclass(slots=True)
class TaskService:
    context: ExecutionContext
    events: EventService
    log_streams: RedisEventStreamRepository
    progress: TaskProgressService
    workspace_changes: WorkspaceChangePublisher | None = None
    callback_dispatcher: TaskCallbackDispatcher | None = None
    async_database: AsyncDatabaseClient | None = None
    async_workspace_changes: AsyncWorkspaceChangeService | None = None
    async_log_streams: AsyncRedisEventStreamRepository | None = None

    def create(
        self,
        name: str,
        *,
        workspace_id: str | None = None,
        app_id: str | None = None,
        stub_id: str | None = None,
        deployment_id: str | None = None,
        container_id: str | None = None,
        parent_task_id: str | None = None,
        root_task_id: str | None = None,
        handler: str | None = None,
        command: list[str] | None = None,
        args: list[JsonValue] | None = None,
        kwargs: dict[str, JsonValue] | None = None,
        invocation: FunctionInvocationPayload | None = None,
        retry_policy: RetryPolicy | Mapping[str, JsonValue] | None = None,
        claimable_at: datetime | None = None,
    ) -> Task:
        with self.context.database.session() as session:
            task = self.create_in_transaction(
                session,
                name,
                workspace_id=workspace_id,
                app_id=app_id,
                stub_id=stub_id,
                deployment_id=deployment_id,
                container_id=container_id,
                parent_task_id=parent_task_id,
                root_task_id=root_task_id,
                handler=handler,
                command=command,
                args=args,
                kwargs=kwargs,
                invocation=invocation,
                retry_policy=retry_policy,
                claimable_at=claimable_at,
            )
        self.publish_created(task)
        return task

    def create_in_transaction(
        self,
        session: DatabaseSession,
        name: str,
        *,
        workspace_id: str | None = None,
        app_id: str | None = None,
        stub_id: str | None = None,
        deployment_id: str | None = None,
        container_id: str | None = None,
        parent_task_id: str | None = None,
        root_task_id: str | None = None,
        handler: str | None = None,
        command: list[str] | None = None,
        args: list[JsonValue] | None = None,
        kwargs: dict[str, JsonValue] | None = None,
        invocation: FunctionInvocationPayload | None = None,
        retry_policy: RetryPolicy | Mapping[str, JsonValue] | None = None,
        claimable_at: datetime | None = None,
    ) -> Task:
        resolved_retry_policy = _task_retry_policy(retry_policy)
        task_id = str(uuid4())
        return self.create_batch_in_transaction(
            session,
            [
                Task(
                    id=task_id,
                    name=name,
                    workspace_id=workspace_id,
                    app_id=app_id,
                    stub_id=stub_id,
                    deployment_id=deployment_id,
                    container_id=container_id,
                    parent_task_id=parent_task_id,
                    root_task_id=root_task_id,
                    handler=handler,
                    command=command or [],
                    args=args or [],
                    kwargs=dict(kwargs or {}),
                    invocation=invocation,
                    claimable_at=claimable_at,
                    retry_policy=resolved_retry_policy,
                    max_attempts=resolved_retry_policy.max_attempts
                    if resolved_retry_policy is not None
                    else 1,
                )
            ],
        )[0]

    def create_batch_in_transaction(
        self, session: DatabaseSession, tasks: Sequence[Task]
    ) -> list[Task]:
        if not tasks:
            return []
        default_workspace = (
            self.context.default_workspace_id(session)
            if any(task.workspace_id is None for task in tasks)
            else None
        )
        prepared = [
            Task.model_validate(
                dict(task)
                | {
                    "id": required_uuid(task.id, field="task_id"),
                    "workspace_id": optional_uuid(
                        task.workspace_id or default_workspace, field="workspace_id"
                    ),
                    "app_id": optional_uuid(task.app_id, field="app_id"),
                    "stub_id": optional_uuid(task.stub_id, field="stub_id"),
                    "deployment_id": optional_uuid(task.deployment_id, field="deployment_id"),
                    "container_id": optional_uuid(task.container_id, field="container_id"),
                    "parent_task_id": optional_uuid(task.parent_task_id, field="parent_task_id"),
                    "root_task_id": optional_uuid(
                        task.root_task_id or task.id, field="root_task_id"
                    ),
                }
            )
            for task in tasks
        ]
        for stub_id in sorted({task.stub_id for task in prepared if task.stub_id is not None}):
            CleanupRepository(session).assert_stub_available(stub_id)
        containers = ContainerRepository(session).statuses_for_ids(
            [task.container_id for task in prepared if task.container_id is not None]
        )
        for task in prepared:
            if task.container_id not in containers:
                task.container_id = None
        saved = TaskRepository(session).upsert_many(prepared)
        self.events.emit_many_in_session(
            session,
            [
                (
                    Event(
                        id=str(uuid4()),
                        action="task.created",
                        resource_type="task",
                        resource_id=task.id,
                        message=f"created task {task.name}",
                    ),
                    task.workspace_id,
                )
                for task in saved
            ],
        )
        return saved

    def publish_created(self, task: Task) -> None:
        self.publish_lifecycle_change(task, WorkspaceChangeType.Created)

    def save(self, task: Task) -> Task:
        """System-authority write; task ownership comes from the record."""
        with self.context.database.session() as session:
            return TaskRepository(session).upsert(
                task,
                workspace_id=task.workspace_id,
            )

    def append_logs(self, task_id: str, stream: str, messages: list[str]) -> None:
        task = self.get(task_id)
        with self.context.database.session() as session:
            container = (
                ContainerRepository(session).get_across_workspaces(task.container_id)
                if task.container_id
                else None
            )
            repository = LogRepository(session)
            entries = [
                repository.append(
                    LogEntry.model_validate(
                        {
                            "id": str(uuid4()),
                            "task_id": required_uuid(task_id, field="task_id"),
                            "container_id": task.container_id,
                            "app_id": task.app_id,
                            "stub_id": task.stub_id,
                            "deployment_id": task.deployment_id,
                            "machine_id": (
                                container.runtime_machine_id or container.machine_id
                                if container is not None
                                else None
                            ),
                            "worker_id": (
                                container.runtime_worker_id or container.worker_id
                                if container is not None
                                else None
                            ),
                            "stream": stream,
                            "message": message.rstrip("\n"),
                        }
                    ),
                    workspace_id=task.workspace_id,
                )
                for message in messages
            ]
        for entry in entries:
            self.log_streams.append_event(
                EventRecordType.ContainerLog,
                {
                    "workspace_id": task.workspace_id or "",
                    "task_id": task.id,
                    "stub_id": task.stub_id or "",
                    "app_id": task.app_id or "",
                    "deployment_id": task.deployment_id or "",
                    "container_id": task.container_id or "",
                    "machine_id": entry.machine_id or "",
                    "worker_id": entry.worker_id or "",
                    "stream": entry.stream,
                    "message": entry.message,
                    "timestamp": entry.created_at.isoformat(),
                },
                event_id=entry.id,
            )

    def transition(
        self,
        task: Task,
        status: TaskStatus,
        *,
        container_id: str | None = None,
        result: JsonValue = None,
        function_result: FunctionResultPayload | None = None,
        error: str | None = None,
        exit_code: int | None = None,
    ) -> Task:
        """Move a task to its next status, from whatever the row currently holds.

        The caller's copy is not what gets written. A task is advanced by more
        than one party, the dispatcher marking it running and binding it to a
        container and the caller finishing it, so saving the object the caller
        happens to be holding silently reverts every field written since it was
        read. That is how endpoint tasks lost the container and start time the
        dispatcher had already recorded.
        """
        if status is TaskStatus.Running:
            return self._start_task(
                task, container_id=container_id or task.container_id, claim=True
            )
        with self.context.database.session() as session:
            updated = self._transition_in_session(
                session,
                task,
                status,
                result=result,
                function_result=function_result,
                error=error,
                exit_code=exit_code,
            )
            callback_url = TaskCallbackService.target_in_session(session, updated)
        self.events.emit(
            f"task.{status.value}",
            resource_type="task",
            resource_id=task.id,
            message=f"task {task.name} {status.value}",
            level=_task_event_level(status),
            workspace_id=task.workspace_id,
        )
        self.publish_lifecycle_change(updated, WorkspaceChangeType.Updated)
        if callback_url is not None:
            self._deliver_callback(updated, callback_url)
        return updated

    async def transition_async(
        self,
        task: Task,
        status: TaskStatus,
        *,
        container_id: str | None = None,
        result: JsonValue = None,
        function_result: FunctionResultPayload | None = None,
        error: str | None = None,
        exit_code: int | None = None,
    ) -> Task:
        if status is TaskStatus.Running:
            return await self._start_task_async(
                task,
                container_id=container_id or task.container_id,
                claim=True,
            )
        database = self._async_database()

        def transition(session: DatabaseSession) -> tuple[Task, str | None]:
            updated = self._transition_in_session(
                session,
                task,
                status,
                result=result,
                function_result=function_result,
                error=error,
                exit_code=exit_code,
            )
            return updated, TaskCallbackService.target_in_session(session, updated)

        updated, callback_url = await database.run_transaction(transition)
        await self.events.emit_async(
            f"task.{status.value}",
            resource_type="task",
            resource_id=task.id,
            message=f"task {task.name} {status.value}",
            level=_task_event_level(status),
            workspace_id=task.workspace_id,
        )
        await self.publish_lifecycle_change_async(updated, WorkspaceChangeType.Updated)
        if callback_url is not None:
            await asyncio.to_thread(self._deliver_callback, updated, callback_url)
        return updated

    @staticmethod
    def _transition_in_session(
        session: DatabaseSession,
        task: Task,
        status: TaskStatus,
        *,
        result: JsonValue,
        function_result: FunctionResultPayload | None,
        error: str | None,
        exit_code: int | None,
    ) -> Task:
        task_repository = TaskRepository(session)
        current = task_repository.get_for_update_across_workspaces(task.id)
        if current is None:
            raise NotFoundError(f"task not found: {task.id}")
        current.status = status
        if is_terminal_task_status(status):
            current.finished_at = utc_now()
        current.result = result
        current.function_result = function_result
        current.error = error
        current.exit_code = exit_code
        updated = task_repository.upsert(
            current,
            workspace_id=current.workspace_id,
        )
        if status is not TaskStatus.Pending:
            attempts = TaskAttemptRepository(session)
            latest = attempts.latest_for_task(task.id)
            if latest is not None:
                latest.status = status
                if updated.container_id:
                    latest.container_id = updated.container_id
                if status is TaskStatus.Running and latest.started_at is None:
                    latest.started_at = utc_now()
                if status is TaskStatus.Retry or is_terminal_task_status(status):
                    latest.finished_at = utc_now()
                    latest.result = result
                    latest.error = error
                    latest.exit_code = exit_code
                attempts.upsert(latest)
        return updated

    def claim_and_start(
        self, stub_id: str, *, workspace_id: str, container_id: str, claim_id: str | None = None
    ) -> TaskClaimOutcome:
        resolved_container_id = required_uuid(container_id, field="container_id")
        resolved_claim_id = optional_uuid(claim_id, field="claim_id")
        persisted: _TaskStartPersistence | None = None
        try:
            with self.context.database.session() as session:
                if not StubRepository(session).exists(
                    stub_id, workspace_id=workspace_id, kind=StubKind.Function
                ):
                    raise NotFoundError("function not found")
                claimed = TaskRepository(session).claim_for_stub(
                    stub_id,
                    workspace_id=workspace_id,
                    container_id=resolved_container_id,
                    limit=1,
                    claim_id=resolved_claim_id,
                )
                if claimed.tasks:
                    [task] = claimed.tasks
                    if task.status is TaskStatus.Running:
                        return TaskClaimOutcome(task, queue_checked=claimed.queue_checked)
                    persisted = self._start_locked_task(
                        session,
                        task,
                        resolved_container_id=resolved_container_id,
                        claim=True,
                        claim_id=resolved_claim_id,
                    )
        except TaskClaimCollisionError:
            if resolved_claim_id is None:
                raise
            # The unique-index conflict waits for the winning transaction to
            # commit. Rolling back also releases the loser's candidate task.
            with self.context.database.session() as session:
                prior = TaskRepository(session).replay_function_claim(
                    stub_id,
                    workspace_id=workspace_id,
                    container_id=resolved_container_id,
                    claim_id=resolved_claim_id,
                )
            return TaskClaimOutcome(
                prior.tasks[0] if prior is not None and prior.tasks else None, queue_checked=False
            )
        if persisted is not None:
            self._publish_task_started(persisted)
            return TaskClaimOutcome(persisted.task, queue_checked=claimed.queue_checked)
        if resolved_claim_id is not None and claimed.queue_checked:
            # An empty SKIP LOCKED result may be a retry racing its own first
            # claim. Release SHARE before waiting for outstanding claims.
            with self.context.database.session() as session:
                ContainerRolloutRepository(session).lock_function_claim_replay(
                    resolved_container_id, stub_id=stub_id
                )
                prior = TaskRepository(session).replay_function_claim(
                    stub_id,
                    workspace_id=workspace_id,
                    container_id=resolved_container_id,
                    claim_id=resolved_claim_id,
                )
            if prior is not None:
                return TaskClaimOutcome(
                    prior.tasks[0] if prior.tasks else None, queue_checked=False
                )
        return TaskClaimOutcome(None, queue_checked=claimed.queue_checked)

    def _start_task(self, task: Task, *, container_id: str | None = None, claim: bool) -> Task:
        resolved_container_id = optional_uuid(container_id, field="container_id")
        with self.context.database.session() as session:
            persisted = self._start_task_in_session(
                session,
                task,
                resolved_container_id=resolved_container_id,
                claim=claim,
            )
        self._publish_task_started(persisted)
        return persisted.task

    @staticmethod
    def _start_task_in_session(
        session: DatabaseSession,
        task: Task,
        *,
        resolved_container_id: str | None,
        claim: bool,
        claim_id: str | None = None,
    ) -> _TaskStartPersistence:
        task_repository = TaskRepository(session)
        current = task_repository.get_for_update_across_workspaces(task.id)
        if current is None:
            raise NotFoundError(f"task not found: {task.id}")
        return TaskService._start_locked_task(
            session,
            current,
            resolved_container_id=resolved_container_id,
            claim=claim,
            claim_id=claim_id,
        )

    @staticmethod
    def _start_locked_task(
        session: DatabaseSession,
        current: Task,
        *,
        resolved_container_id: str | None,
        claim: bool,
        claim_id: str | None = None,
    ) -> _TaskStartPersistence:
        return TaskService._start_locked_tasks(
            session, [(current, resolved_container_id)], claim=claim, claim_id=claim_id
        )[0]

    @staticmethod
    def _start_locked_tasks(
        session: DatabaseSession,
        assignments: Sequence[tuple[Task, str | None]],
        *,
        claim: bool,
        claim_id: str | None = None,
    ) -> list[_TaskStartPersistence]:
        if not assignments:
            return []
        containers = ContainerRepository(session).statuses_for_ids(
            [container_id for _, container_id in assignments if container_id is not None]
        )
        latest = TaskAttemptRepository(session).latest_for_tasks(
            [task.id for task, _ in assignments]
        )
        decisions = [
            TaskService._plan_task_start(
                task,
                resolved_container_id=container_id,
                container_status=containers.get(container_id) if container_id else None,
                latest=latest.get(task.id),
                claim=claim,
            )
            for task, container_id in assignments
        ]
        changed = [decision for decision in decisions if decision.attempt is not None]
        saved = {
            task.id: task
            for task in TaskRepository(session).upsert_many(
                [decision.persisted.task for decision in changed]
            )
        }
        TaskAttemptRepository(session).upsert_many(
            [(decision.attempt, claim_id) for decision in changed if decision.attempt is not None]
        )
        persisted = [
            _TaskStartPersistence(
                task=saved.get(decision.persisted.task.id, decision.persisted.task),
                attempt_started=decision.persisted.attempt_started,
                attempt_container_id=decision.persisted.attempt_container_id,
            )
            for decision in decisions
        ]
        EventService.emit_many_in_session(
            session,
            [
                event
                for result, decision in zip(persisted, decisions, strict=True)
                if decision.record_running
                for event in TaskService._task_started_events(result)
            ],
        )
        return persisted

    @staticmethod
    def _plan_task_start(
        current: Task,
        *,
        resolved_container_id: str | None,
        container_status: ContainerStatus | None,
        latest: TaskAttempt | None,
        claim: bool,
    ) -> _TaskStartDecision:
        if is_terminal_task_status(current.status):
            raise ConflictError(f"task {current.id} is already {current.status.value}")
        assigned_container_id = current.container_id or ""
        if (
            claim
            and resolved_container_id
            and assigned_container_id
            and assigned_container_id != resolved_container_id
        ):
            raise ConflictError(
                f"task {current.id} is assigned to container {assigned_container_id}, "
                f"not {resolved_container_id}"
            )
        if claim and container_status in TERMINAL_CONTAINER_STATUSES:
            raise ConflictError(
                f"container {resolved_container_id} is no longer running and "
                f"cannot claim task {current.id}"
            )
        persisted_container_id = resolved_container_id if container_status is not None else None
        active_attempt = latest is not None and latest.status in {
            TaskStatus.Pending,
            TaskStatus.Running,
        }
        if current.status is TaskStatus.Running and active_attempt:
            changed_attempt = None
            if (
                not claim
                and persisted_container_id
                and current.container_id != persisted_container_id
            ):
                current.container_id = persisted_container_id
                if latest is not None:
                    latest.container_id = persisted_container_id
                    changed_attempt = latest
            return _TaskStartDecision(
                persisted=_TaskStartPersistence(
                    task=current,
                    attempt_started=False,
                    attempt_container_id=current.container_id,
                ),
                attempt=changed_attempt,
                record_running=False,
            )
        now = utc_now()
        if not active_attempt:
            current.attempt_number = max(current.attempt_number, 0) + 1
        current.next_retry_at = None
        if persisted_container_id:
            current.container_id = persisted_container_id
        attempt_container_id = current.container_id
        current.status = TaskStatus.Running
        current.started_at = current.started_at or now
        current.result = None
        current.function_result = None
        current.error = None
        current.exit_code = None
        attempt_started = False
        if active_attempt and latest is not None:
            latest.status = TaskStatus.Running
            latest.started_at = latest.started_at or now
            if attempt_container_id:
                latest.container_id = attempt_container_id
            attempt = latest
        else:
            attempt = TaskAttempt(
                id=str(uuid4()),
                task_id=required_uuid(current.id, field="task_id"),
                workspace_id=current.workspace_id,
                container_id=attempt_container_id,
                attempt_number=current.attempt_number,
                status=TaskStatus.Running,
                started_at=now,
            )
            attempt_started = True
        return _TaskStartDecision(
            persisted=_TaskStartPersistence(
                task=current,
                attempt_started=attempt_started,
                attempt_container_id=attempt_container_id,
            ),
            attempt=attempt,
            record_running=True,
        )

    @staticmethod
    def _task_started_events(persisted: _TaskStartPersistence) -> list[tuple[Event, str | None]]:
        saved = persisted.task
        events: list[tuple[Event, str | None]] = []
        if persisted.attempt_started:
            events.append(
                (
                    Event(
                        id=str(uuid4()),
                        action="task.attempt.started",
                        resource_type="task",
                        resource_id=saved.id,
                        message=f"started attempt {saved.attempt_number} for task {saved.name}",
                        data={
                            "attempt_number": saved.attempt_number,
                            "container_id": persisted.attempt_container_id or "",
                            "max_attempts": saved.max_attempts,
                        },
                    ),
                    saved.workspace_id,
                )
            )
        events.append(
            (
                Event(
                    id=str(uuid4()),
                    action="task.running",
                    resource_type="task",
                    resource_id=saved.id,
                    message=f"task {saved.name} running",
                ),
                saved.workspace_id,
            )
        )
        return events

    def _publish_task_started(self, persisted: _TaskStartPersistence) -> None:
        self.publish_lifecycle_change(persisted.task, WorkspaceChangeType.Updated)

    async def _start_task_async(
        self,
        task: Task,
        *,
        container_id: str | None = None,
        claim: bool,
    ) -> Task:
        resolved_container_id = optional_uuid(container_id, field="container_id")
        persisted = await self._async_database().run_transaction(
            lambda session: self._start_task_in_session(
                session,
                task,
                resolved_container_id=resolved_container_id,
                claim=claim,
            )
        )
        saved = persisted.task
        await self.publish_lifecycle_change_async(saved, WorkspaceChangeType.Updated)
        return saved

    def attempts(self, task_id: str) -> list[TaskAttempt]:
        with self.context.database.session() as session:
            return TaskAttemptRepository(session).list_for_task(task_id)

    def finish_with_retry(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        container_id: str | None = None,
        result: JsonValue = None,
        function_result: FunctionResultPayload | None = None,
        error: str | None = None,
        exit_code: int | None = None,
        retry_allowed: bool = True,
        attempt_number: int | None = None,
        execution_entry: ExecutionEntryEvidence | None = None,
        claim_id: str | None = None,
        function_workspace_id: str | None = None,
    ) -> TaskFinishOutcome:
        with self.context.database.session() as session:
            outcome = self.finish_in_transaction(
                session,
                task_id,
                status,
                container_id=container_id,
                result=result,
                function_result=function_result,
                error=error,
                exit_code=exit_code,
                retry_allowed=retry_allowed,
                attempt_number=attempt_number,
                execution_entry=execution_entry,
                claim_id=claim_id,
                function_workspace_id=function_workspace_id,
            )
        self.publish_finished(outcome)
        return outcome

    def publish_finished(self, outcome: TaskFinishOutcome) -> None:
        if outcome.state_changed:
            self.publish_lifecycle_change(outcome.task, WorkspaceChangeType.Updated)
            if outcome.callback_url is not None:
                self._deliver_callback(outcome.task, outcome.callback_url)

    async def finish_with_retry_async(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        container_id: str | None = None,
        result: JsonValue = None,
        function_result: FunctionResultPayload | None = None,
        error: str | None = None,
        exit_code: int | None = None,
        retry_allowed: bool = True,
    ) -> TaskFinishOutcome:
        outcome = await self._async_database().run_transaction(
            lambda session: self.finish_in_transaction(
                session,
                task_id,
                status,
                container_id=container_id,
                result=result,
                function_result=function_result,
                error=error,
                exit_code=exit_code,
                retry_allowed=retry_allowed,
            )
        )
        await self.publish_finished_async(outcome)
        return outcome

    async def publish_finished_async(self, outcome: TaskFinishOutcome) -> None:
        if outcome.state_changed:
            await self.publish_lifecycle_change_async(outcome.task, WorkspaceChangeType.Updated)
            if outcome.callback_url is not None:
                await asyncio.to_thread(self._deliver_callback, outcome.task, outcome.callback_url)

    @staticmethod
    def _record_finish_in_session(
        session: DatabaseSession, outcome: TaskFinishOutcome, status: TaskStatus
    ) -> None:
        if not outcome.state_changed:
            return
        updated = outcome.task
        decision = outcome.retry_decision
        EventService.emit_in_session(
            session,
            f"task.{updated.status.value}",
            resource_type="task",
            resource_id=updated.id,
            message=f"task {updated.name} {updated.status.value}",
            level=_task_event_level(updated.status),
            workspace_id=updated.workspace_id,
        )
        if decision.should_retry:
            policy = updated.retry_policy or RetryPolicy(max_attempts=updated.max_attempts)
            EventService.emit_in_session(
                session,
                "task.retry.scheduled",
                resource_type="task",
                resource_id=updated.id,
                message=f"scheduled retry for task {updated.name}",
                data={
                    "attempt_number": updated.attempt_number,
                    "next_attempt_number": decision.next_attempt_number,
                    "max_attempts": policy.max_attempts,
                    "delay_seconds": decision.delay_seconds,
                    "next_retry_at": (
                        updated.next_retry_at.isoformat() if updated.next_retry_at else ""
                    ),
                    "failed_status": status.value,
                },
                workspace_id=updated.workspace_id,
            )

    @staticmethod
    def finish_in_transaction(
        session: DatabaseSession,
        task_id: str,
        status: TaskStatus,
        *,
        container_id: str | None = None,
        result: JsonValue = None,
        function_result: FunctionResultPayload | None = None,
        error: str | None = None,
        exit_code: int | None = None,
        retry_allowed: bool = True,
        attempt_number: int | None = None,
        execution_entry: ExecutionEntryEvidence | None = None,
        claim_id: str | None = None,
        function_workspace_id: str | None = None,
    ) -> TaskFinishOutcome:
        task_repository = TaskRepository(session)
        current = (
            task_repository.get_function_for_update(task_id, workspace_id=function_workspace_id)
            if function_workspace_id is not None
            else task_repository.get_for_update_across_workspaces(task_id)
        )
        if current is None:
            raise NotFoundError(f"task not found: {task_id}")
        assigned_container_id = current.container_id or ""
        attempt_container_id = current.container_id
        if is_terminal_task_status(current.status) or current.status is TaskStatus.Retry:
            acknowledged = False
            if (
                claim_id is not None
                and current.workspace_id
                and container_id
                and (attempt_number is None or attempt_number == current.attempt_number)
            ):
                acknowledged = TaskAttemptRepository(session).has_completed_claim(
                    task_id=current.id,
                    workspace_id=current.workspace_id,
                    container_id=container_id,
                    claim_id=required_uuid(claim_id, field="claim_id"),
                    attempt_number=current.attempt_number,
                )
            return _unchanged_finish_outcome(
                current, "task completion is already recorded", claim_acknowledged=acknowledged
            )
        if container_id and assigned_container_id != container_id:
            return _unchanged_finish_outcome(
                current,
                "completion does not own the active task container",
            )
        if attempt_number is not None and current.attempt_number != attempt_number:
            return _unchanged_finish_outcome(current, "completion does not own the active attempt")
        if claim_id is not None:
            if not current.workspace_id or not container_id:
                raise InvalidInputError("claim completion requires a workspace and container")
            recorded = TaskAttemptRepository(session).accept_claim_completion(
                task_id=current.id,
                workspace_id=current.workspace_id,
                container_id=container_id,
                claim_id=required_uuid(claim_id, field="claim_id"),
                attempt_number=current.attempt_number,
                elapsed_since_entry_seconds=(
                    execution_entry.elapsed_since_entry_seconds if execution_entry else None
                ),
                reported_at=utc_now(),
            )
            if not recorded:
                return _unchanged_finish_outcome(current, "completion does not own the claim")
        elif execution_entry is not None:
            raise InvalidInputError("execution entry requires a claim")
        policy = current.retry_policy or RetryPolicy(max_attempts=current.max_attempts)
        decision = (
            plan_retry(
                policy,
                current_attempt_number=max(current.attempt_number, 1),
                status=status,
            )
            if retry_allowed
            else RetryDecision(
                should_retry=False,
                final_status=status,
                reason="retry is disabled for this workload outcome",
            )
        )
        now = utc_now()
        next_status = TaskStatus.Retry if decision.should_retry else status
        current.status = next_status
        current.result = result
        current.function_result = function_result
        current.error = (error or status.value) if decision.should_retry else error
        current.exit_code = exit_code
        if decision.should_retry:
            current.next_retry_at = now + timedelta(seconds=decision.delay_seconds)
            current.container_id = None
        elif is_terminal_task_status(next_status):
            current.finished_at = now
        updated = task_repository.upsert(
            current,
            workspace_id=current.workspace_id,
        )
        attempts = TaskAttemptRepository(session)
        latest = attempts.latest_for_task(updated.id)
        if latest is not None:
            latest.status = status
            if attempt_container_id:
                latest.container_id = attempt_container_id
            latest.finished_at = now
            latest.result = result
            latest.error = current.error
            latest.exit_code = exit_code
            attempts.upsert(latest)
        outcome = TaskFinishOutcome(
            task=updated,
            retry_decision=decision,
            state_changed=True,
            claim_acknowledged=claim_id is not None,
            callback_url=TaskCallbackService.target_in_session(session, updated),
        )
        TaskService._record_finish_in_session(session, outcome, status)
        return outcome

    def due_retry_tasks(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[Task]:
        current = now or utc_now()
        with self.context.database.session() as session:
            return TaskRepository(session).due_retry_tasks(
                now=current,
                limit=limit,
            )

    def fail_unclaimed_claimable_for_stub(
        self,
        stub_id: str,
        *,
        error: str,
        limit: int = 100,
    ) -> list[Task]:
        """Fail queued work only while it is still free for a container to claim."""

        with self.context.database.session() as session:
            candidates = TaskRepository(session).list_unclaimed_claimable_for_update(
                stub_id=stub_id,
                limit=limit,
            )
            failed = [
                self._transition_in_session(
                    session,
                    task,
                    TaskStatus.Failed,
                    result=None,
                    function_result=None,
                    error=error,
                    exit_code=1,
                )
                for task in candidates
            ]
            callbacks = [TaskCallbackService.target_in_session(session, task) for task in failed]
        for task, callback_url in zip(failed, callbacks, strict=True):
            self.events.emit(
                "task.failed",
                resource_type="task",
                resource_id=task.id,
                message=f"task {task.name} failed",
                level=EventLevel.Error,
                workspace_id=task.workspace_id,
            )
            self.publish_lifecycle_change(task, WorkspaceChangeType.Updated)
            if callback_url is not None:
                self._deliver_callback(task, callback_url)
        return failed

    def list(
        self,
        *,
        status: TaskStatus | None = None,
        workspace_id: str | None = None,
    ) -> list[Task]:
        status_value = status.value if status is not None else None
        with self.context.database.session() as session:
            repository = TaskRepository(session)
            tasks = (
                repository.list(status=status_value, workspace_id=workspace_id)
                if workspace_id is not None
                else repository.list_across_workspaces(status=status_value)
            )
        tasks.sort(key=lambda item: item.created_at, reverse=True)
        return tasks

    def assign(self, task: Task, *, container_id: str) -> Task:
        """Bind a task to the container the control plane picked to serve it.

        The control plane is the only party choosing, so a previous choice it has
        moved on from is not a competing claim and does not fence this one.
        """

        return self._start_task(task, container_id=container_id, claim=False)

    async def assign_async(self, task: Task, *, container_id: str) -> Task:
        return await self._start_task_async(task, container_id=container_id, claim=False)

    def assign_locked_batch_in_transaction(
        self, session: DatabaseSession, assignments: Sequence[tuple[Task, str]]
    ) -> list[Task]:
        return [
            result.task
            for result in self._start_locked_tasks(
                session,
                [
                    (task, required_uuid(container_id, field="container_id"))
                    for task, container_id in assignments
                ],
                claim=False,
            )
        ]

    def start(self, task_id: str, *, container_id: str | None = None) -> Task:
        """Record a container's own claim on a task it was handed.

        Two containers believing they own one task is what the container binding
        refuses here, and only a claim can be the second of those two.
        """

        return self._start_task(self.get(task_id), container_id=container_id, claim=True)

    def finish(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: JsonValue = None,
        error: str | None = None,
        exit_code: int | None = None,
    ) -> Task:
        task = self.get(task_id)
        return self.transition(task, status, result=result, error=error, exit_code=exit_code)

    def get(self, task_id: str) -> Task:
        """System-authority lookup for the execution engine's own control flow."""
        with self.context.database.session() as session:
            return self.get_in_session(session, task_id)

    @staticmethod
    def get_in_session(session: DatabaseSession, task_id: str) -> Task:
        task = TaskRepository(session).get_across_workspaces(task_id)
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        return task

    async def get_async(self, task_id: str) -> Task:
        return await self._async_database().run_transaction(
            lambda session: self.get_in_session(session, task_id)
        )

    def cancel(
        self,
        task_id: str,
        *,
        status: TaskStatus = TaskStatus.Cancelled,
        error: str = "task cancelled",
        exit_code: int | None = None,
    ) -> TaskCancellationOutcome:
        if status not in {TaskStatus.Cancelled, TaskStatus.Expired, TaskStatus.Failed}:
            raise ValueError("task cancellation requires a cancellation, expiry, or failure status")
        with self.context.database.session() as session:
            current = TaskRepository(session).get_for_update_across_workspaces(task_id)
            if current is None:
                raise NotFoundError(f"task not found: {task_id}")
            if is_terminal_task_status(current.status):
                return TaskCancellationOutcome(current, state_changed=False)
            updated = self._transition_in_session(
                session,
                current,
                status,
                result=None,
                function_result=None,
                error=error,
                exit_code=exit_code,
            )
            callback_url = TaskCallbackService.target_in_session(session, updated)
        self.events.emit(
            f"task.{status.value}",
            resource_type="task",
            resource_id=task_id,
            message=f"task {updated.name} {status.value}",
            level=_task_event_level(status),
            workspace_id=updated.workspace_id,
        )
        self.publish_lifecycle_change(updated, WorkspaceChangeType.Updated)
        if callback_url is not None:
            self._deliver_callback(updated, callback_url)
        return TaskCancellationOutcome(updated, state_changed=True)

    def logs(self, task_id: str, *, limit: int = 100) -> list[LogEntry]:
        return [record.entry for record in self.log_page(task_id, limit=limit).data]

    def log_page(
        self,
        task_id: str,
        *,
        limit: int = 100,
        cursor: LogPageCursor | None = None,
    ) -> LogPage:
        with self.context.database.session() as session:
            task = self.get_in_session(session, task_id)
            return self.log_page_in_session(session, task, limit=limit, cursor=cursor)

    @staticmethod
    def log_page_in_session(
        session: DatabaseSession,
        task: Task | TaskProgressSnapshot,
        *,
        limit: int,
        cursor: LogPageCursor | None,
        follow: bool = False,
    ) -> LogPage:
        workspace_id = task.workspace_id
        if not workspace_id:
            raise ConflictError(f"task logs require a workspace-owned task: {task.id}")
        repository = LogRepository(session)
        query = LogStreamQuery(
            workspace_id=workspace_id,
            task_id=task.id,
            start_time=LogRetentionService.cutoff_in_session(session, workspace_id),
        )
        if follow or cursor is not None:
            return repository.page_after(
                query,
                workspace_id=workspace_id,
                limit=limit,
                cursor=cursor,
            )
        return repository.page(query, workspace_id=workspace_id, limit=limit)

    def publish_lifecycle_change(self, task: Task, change: WorkspaceChangeType) -> None:
        if not task.workspace_id:
            return
        self._publish_lifecycle_event(task, change)
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=task.workspace_id,
            topic=WorkspaceChangeTopic.Tasks,
            change=change,
            resource_id=task.id,
            app_id=task.app_id,
            deployment_id=task.deployment_id,
            stub_id=task.stub_id,
            task_id=task.id,
            root_task_id=task.root_task_id,
            container_id=task.container_id,
        )

    async def publish_lifecycle_change_async(
        self,
        task: Task,
        change: WorkspaceChangeType,
    ) -> None:
        if not task.workspace_id:
            return
        if self.async_log_streams is None:
            raise RuntimeError("asynchronous task event streams are not configured")
        event_type, data = _task_lifecycle_event(task, change)
        try:
            await self.async_log_streams.append_event(event_type, data)
        except REDIS_UNAVAILABLE_ERRORS as exc:
            _log_lifecycle_publication_failure(task, exc)
        if self.async_workspace_changes is None:
            return
        await self.async_workspace_changes.emit_change(
            workspace_id=task.workspace_id,
            topic=WorkspaceChangeTopic.Tasks,
            change=change,
            resource_id=task.id,
            app_id=task.app_id,
            deployment_id=task.deployment_id,
            stub_id=task.stub_id,
            task_id=task.id,
            root_task_id=task.root_task_id,
            container_id=task.container_id,
        )

    def _publish_lifecycle_event(self, task: Task, change: WorkspaceChangeType) -> None:
        event_type, data = _task_lifecycle_event(task, change)
        try:
            self.log_streams.append_event(event_type, data)
        except REDIS_UNAVAILABLE_ERRORS as exc:
            _log_lifecycle_publication_failure(task, exc)

    async def publish_created_async(self, task: Task) -> None:
        await self.publish_lifecycle_change_async(task, WorkspaceChangeType.Created)

    def _async_database(self) -> AsyncDatabaseClient:
        if self.async_database is None:
            raise RuntimeError("asynchronous task database is not configured")
        return self.async_database

    def _deliver_callback(self, task: Task, target: str) -> None:
        dispatcher = self.callback_dispatcher
        if dispatcher is None:
            dispatcher = TaskCallbackService(self.context, self.events)
            self.callback_dispatcher = dispatcher
        try:
            dispatcher.deliver(task, target=target)
        except Exception as exc:
            self.events.emit(
                "task.callback.failed",
                resource_type="task",
                resource_id=task.id,
                message="task callback delivery failed before dispatch",
                level=EventLevel.Warning,
                data={
                    "error_type": type(exc).__name__,
                    "task_status": task.status.value,
                },
                workspace_id=task.workspace_id,
            )


def _task_event_level(status: TaskStatus) -> EventLevel:
    if status in {TaskStatus.Failed, TaskStatus.Timeout}:
        return EventLevel.Error
    return EventLevel.Info


def _task_retry_policy(
    policy: RetryPolicy | Mapping[str, JsonValue] | None,
) -> RetryPolicy | None:
    if policy is None:
        return None
    resolved = normalize_retry_policy(policy)
    if resolved.max_attempts == 1 and resolved.delay_seconds == 0:
        return None
    return resolved


def _task_lifecycle_event(
    task: Task, change: WorkspaceChangeType
) -> tuple[EventRecordType, EventDataInput]:
    return (
        EventRecordType.TaskCreated
        if change is WorkspaceChangeType.Created
        else EventRecordType.TaskUpdated,
        {
            "task_id": task.id,
            "workspace_id": task.workspace_id,
            "app_id": task.app_id,
            "stub_id": task.stub_id,
            "deployment_id": task.deployment_id,
            "container_id": task.container_id,
            "root_task_id": task.root_task_id,
            "parent_task_id": task.parent_task_id,
            "status": task.status.value,
            "attempt_number": task.attempt_number,
            "claimable_at": task.claimable_at,
            "max_attempts": task.max_attempts,
            "error": task.error,
            "exit_code": task.exit_code,
            "created_at": task.created_at,
            "updated_at": utc_now(),
        },
    )


def _log_lifecycle_publication_failure(task: Task, exc: Exception) -> None:
    LOGGER.warning(
        "Task lifecycle notification unavailable: task_id=%s status=%s error_type=%s",
        task.id,
        task.status.value,
        type(exc).__name__,
    )


def _unchanged_finish_outcome(
    task: Task, reason: str, *, claim_acknowledged: bool = False
) -> TaskFinishOutcome:
    return TaskFinishOutcome(
        task=task,
        retry_decision=RetryDecision(
            should_retry=False,
            final_status=task.status,
            reason=reason,
        ),
        state_changed=False,
        claim_acknowledged=claim_acknowledged,
        callback_url=None,
    )
