from __future__ import annotations

from pydantic import Field, model_validator

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


__all__ = [
    "DEFAULT_RETRYABLE_TASK_STATUS_SEQUENCE",
    "RetryBackoff",
    "RetryPolicy",
    "TaskPolicy",
    "TaskStatus",
]
