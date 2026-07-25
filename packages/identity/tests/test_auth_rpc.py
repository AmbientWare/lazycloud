from __future__ import annotations

from identity.rpc import (
    RpcRetryDecisionReason,
    RpcStatusCode,
    plan_rpc_retry,
    plan_rpc_retry_attempt,
)


def test_rpc_retry_attempt_matrix_preserves_bounded_terminal_decisions() -> None:
    retry = plan_rpc_retry(max_retries=3, initial_delay_seconds=0.25)
    rows = (
        (RpcStatusCode.Ok, 0, False, False, None, RpcRetryDecisionReason.Success, ""),
        (
            RpcStatusCode.Unavailable,
            0,
            True,
            False,
            0.25,
            RpcRetryDecisionReason.RetryableStatus,
            "",
        ),
        (
            RpcStatusCode.ResourceExhausted,
            1,
            True,
            False,
            0.5,
            RpcRetryDecisionReason.RetryableStatus,
            "",
        ),
        (
            RpcStatusCode.PermissionDenied,
            0,
            False,
            False,
            None,
            RpcRetryDecisionReason.NonRetryableStatus,
            "permission-denied",
        ),
        (
            RpcStatusCode.Unavailable,
            2,
            False,
            True,
            None,
            RpcRetryDecisionReason.MaxRetriesReached,
            "max retries reached",
        ),
    )

    for status, attempt, should_retry, exhausted, delay, reason, error in rows:
        decision = plan_rpc_retry_attempt(status, attempt_index=attempt, retry_plan=retry)
        assert decision.retry is should_retry
        assert decision.exhausted is exhausted
        assert decision.delay_seconds == delay
        assert decision.reason is reason
        assert decision.error_message == error

    clamped = plan_rpc_retry_attempt(
        RpcStatusCode.Unavailable,
        attempt_index=-5,
        retry_plan=plan_rpc_retry(max_retries=2, initial_delay_seconds=1),
    )
    assert clamped.attempt_index == 0
    assert clamped.attempt_number == 1
    assert clamped.retry
    assert clamped.delay_seconds == 1

    zero_retry = plan_rpc_retry_attempt(
        RpcStatusCode.ResourceExhausted,
        attempt_index=0,
        retry_plan=plan_rpc_retry(max_retries=0, initial_delay_seconds=1),
    )
    assert not zero_retry.retry
    assert zero_retry.exhausted
    assert zero_retry.reason is RpcRetryDecisionReason.MaxRetriesReached
