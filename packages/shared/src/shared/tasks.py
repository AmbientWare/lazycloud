from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from pydantic import Field, JsonValue, model_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.function_payloads import (
    FunctionDependencyBinding,
    FunctionInvocationPayload,
    FunctionResultPayload,
    validate_function_dependency_bindings,
)
from shared.timestamps import utc_now


class TaskStatus(StringEnum):
    Pending = "pending"
    Running = "running"
    Retry = "retry"
    Complete = "complete"
    Failed = "failed"
    Expired = "expired"
    Timeout = "timeout"
    Cancelled = "cancelled"


class TaskPolicy(ContractModel):
    timeout_seconds: int | None = None


DEFAULT_RETRYABLE_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.Failed, TaskStatus.Timeout}
)
DEFAULT_RETRYABLE_TASK_STATUS_SEQUENCE: tuple[TaskStatus, ...] = (
    TaskStatus.Failed,
    TaskStatus.Timeout,
)


class RetryBackoff(StringEnum):
    Fixed = "fixed"
    Exponential = "exponential"


class RetryPolicy(ContractModel):
    max_attempts: int = Field(default=1, ge=1)
    delay_seconds: float = Field(default=0.0, ge=0)
    backoff: RetryBackoff = RetryBackoff.Fixed
    max_delay_seconds: float | None = Field(default=None, ge=0)
    retry_on_statuses: tuple[TaskStatus, ...] = Field(
        default_factory=lambda: DEFAULT_RETRYABLE_TASK_STATUS_SEQUENCE
    )

    @model_validator(mode="after")
    def max_delay_must_not_be_lower_than_base_delay(self) -> RetryPolicy:
        if self.max_delay_seconds is not None and self.max_delay_seconds < self.delay_seconds:
            msg = "max_delay_seconds cannot be lower than delay_seconds"
            raise ValueError(msg)
        return self

    @property
    def retry_count(self) -> int:
        return max(self.max_attempts - 1, 0)

    @classmethod
    def from_retries(
        cls,
        retries: int = 0,
        *,
        delay_seconds: float = 0.0,
        retry_on_statuses: Iterable[TaskStatus] | None = None,
    ) -> RetryPolicy:
        return cls(
            max_attempts=max(int(retries), 0) + 1,
            delay_seconds=max(float(delay_seconds), 0.0),
            retry_on_statuses=(
                tuple(dict.fromkeys(retry_on_statuses))
                if retry_on_statuses is not None
                else DEFAULT_RETRYABLE_TASK_STATUS_SEQUENCE
            ),
        )


class RetryDecision(ContractModel):
    should_retry: bool
    final_status: TaskStatus
    next_attempt_number: int = 0
    delay_seconds: float = 0.0
    reason: str = ""


def normalize_retry_policy(
    policy: RetryPolicy | Mapping[str, JsonValue] | None = None,
    *,
    retries: int | None = None,
    delay_seconds: float | None = None,
) -> RetryPolicy:
    resolved = _policy_from_value(policy)
    if retries is None and delay_seconds is None:
        return resolved
    return RetryPolicy(
        max_attempts=(max(int(retries), 0) + 1 if retries is not None else resolved.max_attempts),
        delay_seconds=(
            max(float(delay_seconds), 0.0) if delay_seconds is not None else resolved.delay_seconds
        ),
        backoff=resolved.backoff,
        max_delay_seconds=resolved.max_delay_seconds,
        retry_on_statuses=resolved.retry_on_statuses,
    )


def retry_policy_from_config(
    value: RetryPolicy | JsonValue,
    *,
    default_retries: int = 0,
) -> RetryPolicy:
    if isinstance(value, RetryPolicy):
        return value
    if isinstance(value, Mapping) and value:
        return RetryPolicy.model_validate(dict(value))
    return RetryPolicy.from_retries(default_retries)


def plan_retry(
    policy: RetryPolicy,
    *,
    current_attempt_number: int,
    status: TaskStatus,
) -> RetryDecision:
    attempt_number = max(current_attempt_number, 1)
    if status not in policy.retry_on_statuses:
        return RetryDecision(
            should_retry=False,
            final_status=status,
            reason=f"status {status.value} is not retryable",
        )
    if attempt_number >= policy.max_attempts:
        return RetryDecision(
            should_retry=False,
            final_status=status,
            reason="retry limit reached",
        )
    return RetryDecision(
        should_retry=True,
        final_status=TaskStatus.Retry,
        next_attempt_number=attempt_number + 1,
        delay_seconds=retry_delay_for_attempt(policy, attempt_number),
    )


def retry_delay_for_attempt(policy: RetryPolicy, failed_attempt_number: int) -> float:
    if policy.delay_seconds <= 0:
        return 0.0
    if policy.backoff is RetryBackoff.Exponential:
        delay = policy.delay_seconds * (2.0 ** max(failed_attempt_number - 1, 0))
    else:
        delay = policy.delay_seconds
    if policy.max_delay_seconds is not None:
        return min(delay, policy.max_delay_seconds)
    return delay


def _policy_from_value(
    policy: RetryPolicy | Mapping[str, JsonValue] | None,
) -> RetryPolicy:
    if policy is None:
        return RetryPolicy()
    if isinstance(policy, RetryPolicy):
        return policy
    return RetryPolicy.model_validate(dict(policy))


TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.Complete,
        TaskStatus.Failed,
        TaskStatus.Expired,
        TaskStatus.Timeout,
        TaskStatus.Cancelled,
    }
)

IN_FLIGHT_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.Pending,
        TaskStatus.Running,
        TaskStatus.Retry,
    }
)


def is_terminal_task_status(status: TaskStatus) -> bool:
    return status in TERMINAL_TASK_STATUSES


def is_inflight_task_status(status: TaskStatus) -> bool:
    return status in IN_FLIGHT_TASK_STATUSES


class Task(ContractModel):
    id: str
    name: str
    status: TaskStatus = TaskStatus.Pending
    workspace_id: str | None = None
    app_id: str | None = None
    stub_id: str | None = None
    deployment_id: str | None = None
    container_id: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    handler: str | None = None
    command: list[str] = Field(default_factory=list)
    args: list[JsonValue] = Field(default_factory=list)
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    invocation: FunctionInvocationPayload | None = None
    dependency_bindings: list[FunctionDependencyBinding] = Field(default_factory=list)
    function_result: FunctionResultPayload | None = None
    retry_policy: RetryPolicy | None = None
    attempt_number: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    next_retry_at: datetime | None = None
    claimable_at: datetime | None = None
    """When this task's inputs resolved and it became eligible to run.

    Null while a dependency is still outstanding, so a task that waits on another
    is invisible to a claim until its bindings exist. Set once and never cleared —
    it records that readiness happened, rather than that something acted on it.
    """

    result: JsonValue = None
    error: str | None = None
    exit_code: int | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def validate_bindings(self) -> Task:
        validate_function_dependency_bindings(self.dependency_bindings)
        return self


class TaskAttempt(ContractModel):
    id: str
    task_id: str
    workspace_id: str | None = None
    container_id: str | None = None
    attempt_number: int = Field(ge=1)
    status: TaskStatus = TaskStatus.Pending
    result: JsonValue = None
    error: str | None = None
    exit_code: int | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskDependency(ContractModel):
    id: str = ""
    workspace_id: str | None = None
    task_id: str
    upstream_task_id: str
    parent_task_id: str | None = None
    root_task_id: str | None = None
    edge_type: str = "argument"
    created_at: datetime = Field(default_factory=utc_now)


__all__ = [
    "DEFAULT_RETRYABLE_TASK_STATUSES",
    "IN_FLIGHT_TASK_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "RetryBackoff",
    "RetryDecision",
    "RetryPolicy",
    "Task",
    "TaskAttempt",
    "TaskDependency",
    "TaskPolicy",
    "TaskStatus",
    "is_inflight_task_status",
    "is_terminal_task_status",
    "normalize_retry_policy",
    "plan_retry",
    "retry_delay_for_attempt",
    "retry_policy_from_config",
]
