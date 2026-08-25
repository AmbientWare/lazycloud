"""Bounded retry handling for transient HTTP transport failures.

Client-side polling and streaming loops (task result waits, serve attach,
log follow) must survive short control-plane restarts without masking real
API failures. This module owns the shared classification of transient
transport errors and a bounded exponential-backoff tracker used by those
loops. Real HTTP responses (4xx/5xx) surface as ``HttpApiError`` and are
never retried here.
"""

from __future__ import annotations

import http.client
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")

TRANSIENT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    urllib.error.URLError,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
)


def is_transient_transport_error(exc: BaseException) -> bool:
    """True when the error is a network-level blip worth retrying.

    Connection resets/refusals, socket timeouts, truncated reads, and
    connect-phase ``URLError`` failures are transient. ``HTTPError`` is a
    real server response and is never transient.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, TRANSIENT_TRANSPORT_ERRORS)


@dataclass(frozen=True, slots=True)
class TransientRetryPolicy:
    """Bounds for consecutive transient-failure retries in one loop."""

    max_attempts: int = 12
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    budget_seconds: float = 60.0

    def delay_for(self, failure_count: int) -> float:
        delay = self.base_delay_seconds * (2 ** max(failure_count - 1, 0))
        return min(delay, self.max_delay_seconds)


DEFAULT_TRANSIENT_RETRY_POLICY = TransientRetryPolicy()


@dataclass(slots=True)
class TransientRetry:
    """Tracks consecutive transient failures for a polling/streaming loop.

    Call :meth:`backoff` when an attempt fails: it sleeps before the next
    attempt, or re-raises the error when it is not transient or the retry
    budget (attempts, elapsed budget, or the caller's deadline) is
    exhausted. Call :meth:`reset` after every successful attempt so the
    budget applies to consecutive failures only.
    """

    policy: TransientRetryPolicy = DEFAULT_TRANSIENT_RETRY_POLICY
    deadline: float | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _failures: int = field(default=0, init=False)
    _first_failure_at: float | None = field(default=None, init=False)

    def reset(self) -> None:
        self._failures = 0
        self._first_failure_at = None

    def backoff(self, exc: BaseException) -> None:
        if not is_transient_transport_error(exc):
            raise exc
        now = self.clock()
        if self._first_failure_at is None:
            self._first_failure_at = now
        self._failures += 1
        if self._failures >= self.policy.max_attempts:
            raise exc
        if now - self._first_failure_at >= self.policy.budget_seconds:
            raise exc
        delay = self.policy.delay_for(self._failures)
        if self.deadline is not None:
            remaining = self.deadline - now
            if remaining <= 0:
                raise exc
            delay = min(delay, remaining)
        if delay > 0:
            self.sleep(delay)


def call_with_transient_retry(
    fn: Callable[[], T],
    *,
    policy: TransientRetryPolicy = DEFAULT_TRANSIENT_RETRY_POLICY,
    deadline: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run one idempotent request with bounded transient-failure retries."""
    retry = TransientRetry(policy=policy, deadline=deadline, sleep=sleep)
    while True:
        try:
            return fn()
        except TRANSIENT_TRANSPORT_ERRORS as exc:
            retry.backoff(exc)


__all__ = [
    "DEFAULT_TRANSIENT_RETRY_POLICY",
    "TRANSIENT_TRANSPORT_ERRORS",
    "TransientRetry",
    "TransientRetryPolicy",
    "call_with_transient_retry",
    "is_transient_transport_error",
]
