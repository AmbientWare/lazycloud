from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scheduler.fleet import (
    DEFAULT_SCHEDULE_RETRY_GRACE,
    SchedulerRequeueAction,
    SchedulerRetryReason,
    plan_retry_soon,
)


def test_retry_count_cannot_fail_a_request_before_capacity_had_time_to_arrive() -> None:
    """A machine takes minutes to boot; a retry count is reached in seconds.

    The request that triggers a scale-up sees only the machines that exist at
    that moment, so it must keep retrying until the one it asked for could have
    registered, and only then may the count decide it will never fit.
    """
    created = datetime(2026, 1, 1, tzinfo=UTC)

    early = plan_retry_soon(
        retry_count=10,
        request_created_at=created,
        now=created + timedelta(seconds=36),
        max_retry_count=10,
    )
    assert early.action is SchedulerRequeueAction.Requeue
    assert early.reason is SchedulerRetryReason.ScheduleFailed
    assert early.next_retry_count == 11

    late = plan_retry_soon(
        retry_count=10,
        request_created_at=created,
        now=created + DEFAULT_SCHEDULE_RETRY_GRACE,
        max_retry_count=10,
    )
    assert late.action is SchedulerRequeueAction.Fail
    assert late.reason is SchedulerRetryReason.RetryLimit


def test_retry_delay_backs_off_and_stays_bounded() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    delays = [
        plan_retry_soon(
            retry_count=count,
            request_created_at=created,
            now=created + timedelta(seconds=1),
            processing_interval=timedelta(seconds=1),
        ).delay_seconds
        for count in (0, 1, 4, 30)
    ]
    assert delays == [1.0, 2.0, 5.0, 5.0]
