from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import JsonValue
from shared.events import Event, EventLevel


class BillingEventSink(Protocol):
    """Where this package records what an operator has to answer for."""

    def emit(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        message: str,
        level: EventLevel = EventLevel.Info,
        data: dict[str, JsonValue] | None = None,
        workspace_id: str | None = None,
    ) -> Event: ...


def next_attempt_at(
    now: datetime,
    attempts: int,
    *,
    base: timedelta,
    cap: timedelta,
    jitter: float = 0.2,
    max_doublings: int = 16,
) -> datetime:
    """When a refused row is offered again.

    Exponential and capped so a provider outage is not hammered, jittered so a
    batch refused together does not come back together. The bound is the caller's
    because it is the caller's constraint: a meter event has to fit inside the
    provider's deduplication window, and a plan change has only the customer's
    patience.
    """

    doublings = min(max(attempts - 1, 0), max_doublings)
    delay = min(cap, base * 2**doublings)
    return now + delay * (1 + random.uniform(-jitter, jitter))


__all__ = ["BillingEventSink", "next_attempt_at"]
