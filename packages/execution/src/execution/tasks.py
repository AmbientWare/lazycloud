from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.cleanup import CleanupRepository
from database.repositories.execution import (
    LogPage,
    LogPageCursor,
    LogRepository,
    TaskAttemptRepository,
    TaskRepository,
)
from database.repositories.orchestration import ContainerRepository
from database.types import DatabaseSession
from foundation.ids import optional_uuid, required_uuid
from observability.events import EventService
from observability.log_retention import LogRetentionService
from observability.stream_state import RedisEventStreamRepository
from observability.workspace_changes import AsyncWorkspaceChangeService, WorkspaceChangePublisher
from pydantic import JsonValue
from shared.containers import TERMINAL_CONTAINER_STATUSES
from shared.errors import ConflictError, NotFoundError
from shared.events import EventLevel
from shared.function_payloads import FunctionInvocationPayload, FunctionResultPayload
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.logs import LogEntry
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import LogStreamQuery
from shared.tasks import (
    RetryDecision,
    RetryPolicy,
    Task,
    TaskAttempt,
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


@dataclass(frozen=True, slots=True)
class TaskFinishOutcome:
    task: Task
    retry_decision: RetryDecision
    state_changed: bool


@dataclass(frozen=True, slots=True)
class _TaskStartPersistence:
    task: Task
    attempt_started: bool
    attempt_container_id: str | None


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
    ) -> Task:
        resolved_deployment_id = optional_uuid(deployment_id, field="deployment_id")
        resolved_app_id = optional_uuid(app_id, field="app_id")
        resolved_stub_id = optional_uuid(stub_id, field="stub_id")
        resolved_container_id = optional_uuid(container_id, field="container_id")
        resolved_parent_task_id = optional_uuid(parent_task_id, field="parent_task_id")
        resolved_root_task_id = optional_uuid(root_task_id, field="root_task_id")
        resolved_retry_policy = _task_retry_policy(retry_policy)
        if resolved_stub_id is not None:
            CleanupRepository(session).assert_stub_available(resolved_stub_id)
        resolved_workspace_id = workspace_id or self.context.default_workspace_id(session)
        persisted_container_id = (
            resolved_container_id if _container_exists(session, resolved_container_id) else None
        )
        return TaskRepository(session).records.create_across_workspaces(
            {
                "name": name,
                "workspace_id": resolved_workspace_id,
                "app_id": resolved_app_id,
                "stub_id": resolved_stub_id,
                "deployment_id": resolved_deployment_id,
                "container_id": persisted_container_id,
                "parent_task_id": resolved_parent_task_id,
                "root_task_id": resolved_root_task_id,
                "handler": handler,
                "command": command or [],
                "args": args or [],
                "kwargs": dict(kwargs or {}),
                "invocation": (
                    invocation.model_dump(mode="json") if invocation is not None else None
                ),
                "retry_policy": (
                    resolved_retry_policy.model_dump(mode="json")
                    if resolved_retry_policy is not None
                    else None
                ),
                "attempt_number": 0,
                "max_attempts": (
                    resolved_retry_policy.max_attempts if resolved_retry_policy is not None else 1
                ),
                "status": TaskStatus.Pending.value,
            },
            name=name,
            status=TaskStatus.Pending.value,
        )

    def publish_created(self, task: Task) -> None:
        self.events.emit(
            "task.created",
            resource_type="task",
            resource_id=task.id,
            message=f"created task {task.name}",
            workspace_id=task.workspace_id,
        )
        self.publish_lifecycle_change(task, WorkspaceChangeType.Created)

    def save(self, task: Task) -> Task:
        """System-authority write; task ownership comes from the record."""
        with self.context.database.session() as session:
            return TaskRepository(session).records.upsert_across_workspaces(
                task,
                workspace_id=task.workspace_id,
                name=task.name,
                status=task.status.value,
            )

    def append_logs(self, task_id: str, stream: str, messages: list[str]) -> None:
        task = self.get(task_id)
        with self.context.database.session() as session:
            records = LogRepository(session).records
            entries = [
                records.create_across_workspaces(
                    {
                        "task_id": required_uuid(task_id, field="task_id"),
                        "stream": stream,
                        "message": message.rstrip("\n"),
                    },
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
        self.events.emit(
            f"task.{status.value}",
            resource_type="task",
            resource_id=task.id,
            message=f"task {task.name} {status.value}",
            level=_task_event_level(status),
            workspace_id=task.workspace_id,
        )
        self.publish_lifecycle_change(updated, WorkspaceChangeType.Updated)
        self._deliver_callback(updated)
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
        updated = await database.run_transaction(
            lambda session: self._transition_in_session(
                session,
                task,
                status,
                result=result,
                function_result=function_result,
                error=error,
                exit_code=exit_code,
            )
        )
        await self.events.emit_async(
            f"task.{status.value}",
            resource_type="task",
            resource_id=task.id,
            message=f"task {task.name} {status.value}",
            level=_task_event_level(status),
            workspace_id=task.workspace_id,
        )
        await self.publish_lifecycle_change_async(updated, WorkspaceChangeType.Updated)
        await asyncio.to_thread(self._deliver_callback, updated)
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
        updated = task_repository.records.upsert_across_workspaces(
            current,
            workspace_id=current.workspace_id,
            name=current.name,
            status=status.value,
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

    def claim_and_start(self, stub_id: str, *, container_id: str) -> Task | None:
        resolved_container_id = required_uuid(container_id, field="container_id")
        with self.context.database.session() as session:
            claimed = TaskRepository(session).claim_for_stub(
                stub_id, container_id=resolved_container_id, limit=1
            )
            if not claimed:
                return None
            persisted = self._start_task_in_session(
                session,
                claimed[0],
                resolved_container_id=resolved_container_id,
                claim=True,
            )
        self._publish_task_started(persisted)
        return persisted.task

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
    ) -> _TaskStartPersistence:
        task_repository = TaskRepository(session)
        current = task_repository.get_for_update_across_workspaces(task.id)
        if current is None:
            raise NotFoundError(f"task not found: {task.id}")
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
        container = (
            ContainerRepository(session).records.get_across_workspaces(resolved_container_id)
            if resolved_container_id
            else None
        )
        if claim and container is not None and container.status in TERMINAL_CONTAINER_STATUSES:
            raise ConflictError(
                f"container {resolved_container_id} is no longer running and "
                f"cannot claim task {current.id}"
            )
        persisted_container_id = resolved_container_id if container is not None else None
        attempt_repository = TaskAttemptRepository(session)
        latest = attempt_repository.latest_for_task(current.id)
        active_attempt = latest is not None and latest.status in {
            TaskStatus.Pending,
            TaskStatus.Running,
        }
        if current.status is TaskStatus.Running and active_attempt:
            return _TaskStartPersistence(
                task=current,
                attempt_started=False,
                attempt_container_id=current.container_id,
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
        saved = task_repository.records.upsert_across_workspaces(
            current,
            workspace_id=current.workspace_id,
            name=current.name,
            status=TaskStatus.Running.value,
        )
        attempt_started = False
        if active_attempt and latest is not None:
            latest.status = TaskStatus.Running
            latest.started_at = latest.started_at or now
            if attempt_container_id:
                latest.container_id = attempt_container_id
            attempt_repository.upsert(latest)
        else:
            attempt_repository.records.create_across_workspaces(
                {
                    "task_id": required_uuid(saved.id, field="task_id"),
                    "workspace_id": saved.workspace_id,
                    "container_id": attempt_container_id,
                    "attempt_number": saved.attempt_number,
                    "status": TaskStatus.Running.value,
                    "started_at": now,
                },
                workspace_id=saved.workspace_id,
                status=TaskStatus.Running.value,
            )
            attempt_started = True
        return _TaskStartPersistence(
            task=saved,
            attempt_started=attempt_started,
            attempt_container_id=attempt_container_id,
        )

    def _publish_task_started(self, persisted: _TaskStartPersistence) -> None:
        saved = persisted.task
        if persisted.attempt_started:
            self.events.emit(
                "task.attempt.started",
                resource_type="task",
                resource_id=saved.id,
                message=f"started attempt {saved.attempt_number} for task {saved.name}",
                data={
                    "attempt_number": saved.attempt_number,
                    "container_id": persisted.attempt_container_id or "",
                    "max_attempts": saved.max_attempts,
                },
                workspace_id=saved.workspace_id,
            )
        self.events.emit(
            "task.running",
            resource_type="task",
            resource_id=saved.id,
            message=f"task {saved.name} running",
            workspace_id=saved.workspace_id,
        )
        self.publish_lifecycle_change(saved, WorkspaceChangeType.Updated)

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
        if persisted.attempt_started:
            await self.events.emit_async(
                "task.attempt.started",
                resource_type="task",
                resource_id=saved.id,
                message=f"started attempt {saved.attempt_number} for task {saved.name}",
                data={
                    "attempt_number": saved.attempt_number,
                    "container_id": persisted.attempt_container_id or "",
                    "max_attempts": saved.max_attempts,
                },
                workspace_id=saved.workspace_id,
            )
        await self.events.emit_async(
            "task.running",
            resource_type="task",
            resource_id=saved.id,
            message=f"task {saved.name} running",
            workspace_id=saved.workspace_id,
        )
        await self.publish_lifecycle_change_async(saved, WorkspaceChangeType.Updated)
        return saved

    def latest_attempt(self, task_id: str) -> TaskAttempt | None:
        with self.context.database.session() as session:
            return TaskAttemptRepository(session).latest_for_task(task_id)

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
    ) -> TaskFinishOutcome:
        with self.context.database.session() as session:
            outcome = self._finish_with_retry_in_session(
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
        if not outcome.state_changed:
            return outcome
        updated = outcome.task
        decision = outcome.retry_decision
        self.events.emit(
            f"task.{updated.status.value}",
            resource_type="task",
            resource_id=updated.id,
            message=f"task {updated.name} {updated.status.value}",
            level=_task_event_level(updated.status),
            workspace_id=updated.workspace_id,
        )
        if decision.should_retry:
            policy = updated.retry_policy or RetryPolicy(max_attempts=updated.max_attempts)
            self.events.emit(
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
        self.publish_lifecycle_change(updated, WorkspaceChangeType.Updated)
        self._deliver_callback(updated)
        return outcome

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
            lambda session: self._finish_with_retry_in_session(
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
        if not outcome.state_changed:
            return outcome
        updated = outcome.task
        decision = outcome.retry_decision
        await self.events.emit_async(
            f"task.{updated.status.value}",
            resource_type="task",
            resource_id=updated.id,
            message=f"task {updated.name} {updated.status.value}",
            level=_task_event_level(updated.status),
            workspace_id=updated.workspace_id,
        )
        if decision.should_retry:
            policy = updated.retry_policy or RetryPolicy(max_attempts=updated.max_attempts)
            await self.events.emit_async(
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
        await self.publish_lifecycle_change_async(updated, WorkspaceChangeType.Updated)
        await asyncio.to_thread(self._deliver_callback, updated)
        return outcome

    @staticmethod
    def _finish_with_retry_in_session(
        session: DatabaseSession,
        task_id: str,
        status: TaskStatus,
        *,
        container_id: str | None,
        result: JsonValue,
        function_result: FunctionResultPayload | None,
        error: str | None,
        exit_code: int | None,
        retry_allowed: bool,
    ) -> TaskFinishOutcome:
        task_repository = TaskRepository(session)
        current = task_repository.get_for_update_across_workspaces(task_id)
        if current is None:
            raise NotFoundError(f"task not found: {task_id}")
        assigned_container_id = current.container_id or ""
        attempt_container_id = current.container_id
        if is_terminal_task_status(current.status):
            return _unchanged_finish_outcome(current, "task is already terminal")
        if current.status is TaskStatus.Retry:
            return _unchanged_finish_outcome(current, "task retry is already recorded")
        if container_id and assigned_container_id != container_id:
            return _unchanged_finish_outcome(
                current,
                "completion does not own the active task container",
            )
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
        updated = task_repository.records.upsert_across_workspaces(
            current,
            workspace_id=current.workspace_id,
            name=current.name,
            status=next_status.value,
        )
        attempts = TaskAttemptRepository(session)
        latest = attempts.latest_for_task(updated.id)
        if latest is not None:
            latest.status = next_status
            if attempt_container_id:
                latest.container_id = attempt_container_id
            latest.finished_at = now
            latest.result = result
            latest.error = current.error
            latest.exit_code = exit_code
            attempts.upsert(latest)
        return TaskFinishOutcome(
            task=updated,
            retry_decision=decision,
            state_changed=True,
        )

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
        for task in failed:
            self.events.emit(
                "task.failed",
                resource_type="task",
                resource_id=task.id,
                message=f"task {task.name} failed",
                level=EventLevel.Error,
                workspace_id=task.workspace_id,
            )
            self.publish_lifecycle_change(task, WorkspaceChangeType.Updated)
            self._deliver_callback(task)
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

    def cancel(self, task_id: str) -> Task:
        task = self.get(task_id)
        if is_terminal_task_status(task.status):
            return task
        return self.transition(task, TaskStatus.Cancelled, error="task cancelled")

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
        task: Task,
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
        if self.workspace_changes is None or not task.workspace_id:
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
        if self.async_workspace_changes is None or not task.workspace_id:
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

    async def publish_created_async(self, task: Task) -> None:
        await self.events.emit_async(
            "task.created",
            resource_type="task",
            resource_id=task.id,
            message=f"created task {task.name}",
            workspace_id=task.workspace_id,
        )
        await self.publish_lifecycle_change_async(task, WorkspaceChangeType.Created)

    def _async_database(self) -> AsyncDatabaseClient:
        if self.async_database is None:
            raise RuntimeError("asynchronous task database is not configured")
        return self.async_database

    def _deliver_callback(self, task: Task) -> None:
        dispatcher = self.callback_dispatcher
        if dispatcher is None:
            dispatcher = TaskCallbackService(self.context, self.events)
            self.callback_dispatcher = dispatcher
        try:
            dispatcher.deliver(task)
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


def _container_exists(session: DatabaseSession, container_id: str | None) -> bool:
    if not container_id:
        return False
    return ContainerRepository(session).records.get_across_workspaces(container_id) is not None


def _task_retry_policy(
    policy: RetryPolicy | Mapping[str, JsonValue] | None,
) -> RetryPolicy | None:
    if policy is None:
        return None
    resolved = normalize_retry_policy(policy)
    if resolved.max_attempts == 1 and resolved.delay_seconds == 0:
        return None
    return resolved


def _unchanged_finish_outcome(task: Task, reason: str) -> TaskFinishOutcome:
    return TaskFinishOutcome(
        task=task,
        retry_decision=RetryDecision(
            should_retry=False,
            final_status=task.status,
            reason=reason,
        ),
        state_changed=False,
    )
