from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import Field, JsonValue, model_validator

from lazycloud._shared.enums import StringEnum
from lazycloud.contracts import ContractModel


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


def _policy_from_value(
    policy: RetryPolicy | Mapping[str, JsonValue] | None,
) -> RetryPolicy:
    if policy is None:
        return RetryPolicy()
    if isinstance(policy, RetryPolicy):
        return policy
    return RetryPolicy.model_validate(dict(policy))


__all__ = [
    "RetryBackoff",
    "RetryPolicy",
    "TaskPolicy",
    "TaskStatus",
    "normalize_retry_policy",
]
