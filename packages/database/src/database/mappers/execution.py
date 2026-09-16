from __future__ import annotations

from shared.cron import CronJobRun
from shared.errors import InvalidInputError
from shared.events import Event, EventLevel
from shared.logs import LogEntry
from shared.tasks import RetryPolicy, Task, TaskAttempt, TaskDependency
from shared.timestamps import to_utc, to_utc_or_none

from database.records.execution import PodUrlRecord
from database.tables.execution import (
    CronJobRunTable,
    EventTable,
    LogTable,
    PodUrlTable,
    TaskAttemptTable,
    TaskDependencyTable,
    TaskTable,
)


def task_from_table(row: TaskTable) -> Task:
    kwargs = dict(row.kwargs)
    if row.input_container_id is not None:
        kwargs["container_id"] = row.input_container_id
    retry_policy = None
    if row.retry_backoff is not None:
        retry_policy = RetryPolicy.model_validate(
            {
                "max_attempts": row.max_attempts,
                "backoff": row.retry_backoff,
                "delay_seconds": row.retry_delay_seconds,
                "max_delay_seconds": row.retry_max_delay_seconds,
                "retry_on_statuses": row.retry_on_statuses,
            }
        )
    return Task.model_validate(
        {
            "id": str(row.id),
            "name": row.name,
            "status": row.status,
            "workspace_id": row.workspace_id,
            "app_id": row.app_id,
            "stub_id": row.stub_id,
            "deployment_id": row.deployment_id,
            "container_id": row.container_id,
            "parent_task_id": row.parent_task_id,
            "root_task_id": row.root_task_id,
            "handler": row.handler,
            "command": row.command,
            "args": row.args,
            "kwargs": kwargs,
            "invocation": row.invocation,
            "dependency_bindings": row.dependency_bindings,
            "function_result": row.function_result,
            "retry_policy": retry_policy,
            "attempt_number": row.attempt_number,
            "max_attempts": row.max_attempts,
            "next_retry_at": to_utc_or_none(row.next_retry_at),
            "claimable_at": to_utc_or_none(row.claimable_at),
            "result": row.result,
            "error": row.error,
            "exit_code": row.exit_code,
            "created_at": to_utc(row.created_at),
            "started_at": to_utc_or_none(row.started_at),
            "finished_at": to_utc_or_none(row.finished_at),
        }
    )


def write_task_row(row: TaskTable, task: Task) -> None:
    row.name = task.name
    row.status = task.status.value
    row.workspace_id = task.workspace_id
    row.app_id = task.app_id
    row.stub_id = task.stub_id
    row.deployment_id = task.deployment_id
    row.container_id = task.container_id
    row.parent_task_id = task.parent_task_id
    row.root_task_id = task.root_task_id
    row.handler = task.handler
    row.command = list(task.command)
    row.args = list(task.args)
    row.kwargs = dict(task.kwargs)
    input_container_id = row.kwargs.get("container_id")
    row.input_container_id = input_container_id if isinstance(input_container_id, str) else None
    if row.input_container_id is not None:
        del row.kwargs["container_id"]
    row.invocation = (
        task.invocation.model_dump(mode="json") if task.invocation is not None else None
    )
    row.dependency_bindings = [
        binding.model_dump(mode="json") for binding in task.dependency_bindings
    ]
    row.function_result = (
        task.function_result.model_dump(mode="json") if task.function_result is not None else None
    )
    policy = task.retry_policy
    if policy is not None and policy.max_attempts != task.max_attempts:
        raise InvalidInputError("task retry limit must match its retry policy")
    row.retry_backoff = policy.backoff.value if policy is not None else None
    row.retry_delay_seconds = policy.delay_seconds if policy is not None else None
    row.retry_max_delay_seconds = policy.max_delay_seconds if policy is not None else None
    row.retry_on_statuses = (
        [status.value for status in policy.retry_on_statuses] if policy is not None else None
    )
    row.attempt_number = task.attempt_number
    row.max_attempts = task.max_attempts
    row.next_retry_at = task.next_retry_at
    row.claimable_at = task.claimable_at
    row.result = task.result
    row.error = task.error
    row.exit_code = task.exit_code
    row.created_at = task.created_at
    row.started_at = task.started_at
    row.finished_at = task.finished_at


def task_attempt_from_table(row: TaskAttemptTable) -> TaskAttempt:
    return TaskAttempt.model_validate(
        {
            "id": str(row.id),
            "task_id": row.task_id,
            "workspace_id": row.workspace_id,
            "container_id": row.container_id,
            "attempt_number": row.attempt_number,
            "status": row.status,
            "result": row.result,
            "error": row.error,
            "exit_code": row.exit_code,
            "created_at": to_utc(row.created_at),
            "started_at": to_utc_or_none(row.started_at),
            "finished_at": to_utc_or_none(row.finished_at),
        }
    )


def write_task_attempt_row(row: TaskAttemptTable, attempt: TaskAttempt) -> None:
    row.task_id = attempt.task_id
    row.workspace_id = attempt.workspace_id
    row.container_id = attempt.container_id
    row.attempt_number = attempt.attempt_number
    row.status = attempt.status.value
    row.result = attempt.result
    row.error = attempt.error
    row.exit_code = attempt.exit_code
    row.created_at = attempt.created_at
    row.started_at = attempt.started_at
    row.finished_at = attempt.finished_at


def task_dependency_from_table(row: TaskDependencyTable) -> TaskDependency:
    return TaskDependency(
        id=str(row.id),
        workspace_id=row.workspace_id,
        task_id=str(row.task_id),
        upstream_task_id=str(row.upstream_task_id),
        parent_task_id=row.parent_task_id,
        root_task_id=row.root_task_id,
        edge_type=row.edge_type,
        created_at=to_utc(row.created_at),
    )


def log_entry_from_table(row: LogTable) -> LogEntry:
    return LogEntry.model_validate(
        {
            "id": str(row.id),
            "task_id": row.task_id,
            "container_id": row.container_id,
            "app_id": row.app_id,
            "deployment_id": row.deployment_id,
            "stub_id": row.stub_id,
            "machine_id": row.machine_id,
            "worker_id": row.worker_id,
            "stream": row.stream,
            "message": row.message,
            "created_at": to_utc(row.created_at),
        }
    )


def write_log_row(row: LogTable, entry: LogEntry) -> None:
    row.task_id = entry.task_id
    row.container_id = entry.container_id
    row.app_id = entry.app_id
    row.deployment_id = entry.deployment_id
    row.stub_id = entry.stub_id
    row.machine_id = entry.machine_id
    row.worker_id = entry.worker_id
    row.stream = entry.stream
    row.message = entry.message
    row.created_at = entry.created_at


def event_from_table(row: EventTable) -> Event:
    data = dict(row.data)
    if row.container_id is not None:
        data["container_id"] = row.container_id
    return Event(
        id=str(row.id),
        action=row.action,
        level=EventLevel(row.level),
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        message=row.message,
        data=data,
        created_at=to_utc(row.created_at),
    )


def write_event_row(row: EventTable, event: Event) -> None:
    row.action = event.action
    row.level = event.level.value
    row.resource_type = event.resource_type
    row.resource_id = event.resource_id
    row.message = event.message
    row.data = dict(event.data)
    container_id = row.data.get("container_id")
    row.container_id = container_id if isinstance(container_id, str) else None
    if row.container_id is not None:
        del row.data["container_id"]
    row.created_at = event.created_at


def cron_job_run_from_table(row: CronJobRunTable) -> CronJobRun:
    return CronJobRun(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        cron_job=row.cron_job,
        enqueued=row.enqueued,
        task_id=row.task_id,
        reason=row.reason,
        created_at=to_utc(row.created_at),
    )


def pod_url_record_from_table(row: PodUrlTable) -> PodUrlRecord:
    return PodUrlRecord(
        id=str(row.id),
        container_id=str(row.container_id),
        port=int(row.port),
        url=row.url,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


__all__ = ["pod_url_record_from_table"]
