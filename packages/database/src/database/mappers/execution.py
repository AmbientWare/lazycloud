from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue
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


def task_row_values(task: Task) -> dict[str, JsonValue | datetime]:
    kwargs = dict(task.kwargs)
    input_container_id = kwargs.get("container_id")
    if isinstance(input_container_id, str):
        del kwargs["container_id"]
    else:
        input_container_id = None
    policy = task.retry_policy
    if policy is not None and policy.max_attempts != task.max_attempts:
        raise InvalidInputError("task retry limit must match its retry policy")
    return {
        "id": task.id,
        "name": task.name,
        "status": task.status.value,
        "workspace_id": task.workspace_id,
        "app_id": task.app_id,
        "stub_id": task.stub_id,
        "deployment_id": task.deployment_id,
        "container_id": task.container_id,
        "parent_task_id": task.parent_task_id,
        "root_task_id": task.root_task_id,
        "handler": task.handler,
        "command": list(task.command),
        "args": list(task.args),
        "kwargs": kwargs,
        "input_container_id": input_container_id,
        "invocation": task.invocation.model_dump(mode="json") if task.invocation else None,
        "dependency_bindings": [item.model_dump(mode="json") for item in task.dependency_bindings],
        "function_result": task.function_result.model_dump(mode="json")
        if task.function_result
        else None,
        "retry_backoff": policy.backoff.value if policy else None,
        "retry_delay_seconds": policy.delay_seconds if policy else None,
        "retry_max_delay_seconds": policy.max_delay_seconds if policy else None,
        "retry_on_statuses": [status.value for status in policy.retry_on_statuses]
        if policy
        else None,
        "attempt_number": task.attempt_number,
        "max_attempts": task.max_attempts,
        "next_retry_at": task.next_retry_at,
        "claimable_at": task.claimable_at,
        "result": task.result,
        "error": task.error,
        "exit_code": task.exit_code,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }


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


def task_attempt_row_values(attempt: TaskAttempt) -> dict[str, JsonValue | datetime]:
    return {
        "id": attempt.id,
        "task_id": attempt.task_id,
        "workspace_id": attempt.workspace_id,
        "container_id": attempt.container_id,
        "attempt_number": attempt.attempt_number,
        "status": attempt.status.value,
        "result": attempt.result,
        "error": attempt.error,
        "exit_code": attempt.exit_code,
        "created_at": attempt.created_at,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
    }


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
