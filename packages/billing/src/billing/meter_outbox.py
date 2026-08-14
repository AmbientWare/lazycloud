from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import partial
from typing import Protocol
from uuid import uuid4

from database.client import DatabaseClient
from database.repositories.billing_outbox import BillingMeterOutboxRepository, ClaimedMeterEvent
from pydantic import JsonValue
from shared.billing_quotes import BilledDimension
from shared.errors import InvalidInputError
from shared.events import Event, EventLevel
from shared.payments import METER_EVENT_NAMES, PaymentProvider
from shared.timestamps import to_utc, utc_now

LOGGER = logging.getLogger(__name__)

METER_EVENT_ABANDONED_ACTION = "billing.meter_event.abandoned"
"""Money that was metered, priced and never reached the provider."""

METER_EVENT_RESOURCE_TYPE = "billing_meter_event"

MAX_ATTEMPTS = 12
"""Attempts a row is given before its charge is given up on.

With the backoff below this spans about an hour, which is the constraint that
matters: a provider deduplicates a resent `identifier` only for a bounded window,
and a schedule that outlasted that window would turn a lost acknowledgement into
a second charge.
"""

CLAIM_TTL = timedelta(minutes=5)
SENT_RETENTION = timedelta(days=7)

_RETRY_BASE = timedelta(seconds=5)
_RETRY_CAP = timedelta(seconds=900)
_RETRY_JITTER = 0.2
_MAX_BACKOFF_DOUBLINGS = 16

_DIMENSIONS_BY_METER_EVENT: Mapping[str, BilledDimension] = {
    name: dimension for dimension, name in METER_EVENT_NAMES.items()
}


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


@dataclass(frozen=True, slots=True)
class MeterEventDrainResult:
    """What one sweep did, in the terms the loop that called it reports."""

    sent_count: int = 0
    retried_count: int = 0
    abandoned_count: int = 0


class _Verdict(Enum):
    Sent = auto()
    Retry = auto()
    Abandon = auto()


@dataclass(frozen=True, slots=True)
class _Outcome:
    event: ClaimedMeterEvent
    verdict: _Verdict
    error: str = ""


@dataclass(frozen=True, slots=True)
class BillingMeterOutboxService:
    """Deliver the meter events the pricer already owed the provider.

    The work set is the rows themselves, never a query over usage. That is the
    whole difference from a sweep that recomputes what it owes on every run: one
    account the provider keeps refusing is paced by its own row and passed over
    by the next claim, so it cannot hold the front of every batch behind it.

    At-least-once, deliberately. A send whose acknowledgement is lost is offered
    again under the same `identifier`, and the provider deduplicates on it — so
    the failure mode is a repeat the provider discards rather than usage nobody
    is charged for.
    """

    database: DatabaseClient
    payments: Callable[[], PaymentProvider]
    events: BillingEventSink
    batch_limit: int = 200
    max_batches: int = 5
    send_concurrency: int = 8
    """Bounded so a backlog cannot open an unbounded number of connections to the
    provider, and so the loop this runs in gets its tick back."""

    def drain(self, *, now: datetime | None = None) -> MeterEventDrainResult:
        """Send what is due, in bounded batches, and settle each row on its own.

        The credential is resolved before anything is claimed. A claim spends an
        attempt, and spending twelve of them on a key this process cannot read
        would abandon a workspace's charges for a reason no retry addresses.
        """

        moment = to_utc(now or utc_now())
        self._reclaim(moment)
        result = MeterEventDrainResult()
        payments = self.payments()
        for _ in range(self.max_batches):
            claim_token = str(uuid4())
            with self.database.session() as session:
                claimed = BillingMeterOutboxRepository(session).claim(
                    now=moment,
                    limit=self.batch_limit,
                    claim_token=claim_token,
                )
            if not claimed:
                break
            result = self._settle(
                self._deliver(payments, claimed),
                now=moment,
                claim_token=claim_token,
                totals=result,
            )
            if len(claimed) < self.batch_limit:
                break
        return result

    def prune(self, *, now: datetime | None = None, limit: int = 1_000) -> int:
        """Delete acknowledged rows past their retention, in one bounded batch.

        Only the acknowledged ones. An abandoned row is the evidence of money
        that never left, and it is kept until somebody has answered for it.
        """

        moment = to_utc(now or utc_now())
        with self.database.session() as session:
            return BillingMeterOutboxRepository(session).prune(
                sent_before=moment - SENT_RETENTION,
                limit=limit,
            )

    def _reclaim(self, now: datetime) -> None:
        with self.database.session() as session:
            BillingMeterOutboxRepository(session).reclaim(
                now=now,
                claimed_before=now - CLAIM_TTL,
            )

    def _deliver(
        self,
        payments: PaymentProvider,
        claimed: Sequence[ClaimedMeterEvent],
    ) -> list[_Outcome]:
        with ThreadPoolExecutor(
            max_workers=min(self.send_concurrency, len(claimed)),
            thread_name_prefix="meter-outbox",
        ) as pool:
            return list(pool.map(partial(self._send, payments), claimed))

    def _send(self, payments: PaymentProvider, event: ClaimedMeterEvent) -> _Outcome:
        """Offer one event, answering with what should become of its row.

        Every failure is caught here because the batch is a set of independent
        rows and one provider refusal is not a statement about the others. What
        is caught is never lost: a retryable failure is written to the row it
        belongs to and paced, and a terminal one leaves a durable error event.
        """

        try:
            payments.record_meter_event(
                event_name=event.meter_event_name,
                provider_customer_id=event.provider_customer_id,
                value_nanos=event.value_nanos,
                occurred_at=event.occurred_at,
                identifier=event.identifier,
                pricing_version=event.pricing_version,
            )
        except InvalidInputError as error:
            # The provider will refuse this payload every time it is offered —
            # a malformed event, or one whose timestamp has fallen out of the
            # window they accept usage for. Retrying it is the unpaced retry this
            # design exists to avoid.
            return _Outcome(event=event, verdict=_Verdict.Abandon, error=str(error))
        except Exception as error:
            LOGGER.warning(
                "billing: meter event %s was not accepted (attempt %d): %s",
                event.identifier,
                event.attempts,
                error,
            )
            if event.attempts >= MAX_ATTEMPTS:
                return _Outcome(
                    event=event,
                    verdict=_Verdict.Abandon,
                    error=f"gave up after {event.attempts} attempts: {error}",
                )
            return _Outcome(event=event, verdict=_Verdict.Retry, error=str(error))
        return _Outcome(event=event, verdict=_Verdict.Sent)

    def _settle(
        self,
        outcomes: Sequence[_Outcome],
        *,
        now: datetime,
        claim_token: str,
        totals: MeterEventDrainResult,
    ) -> MeterEventDrainResult:
        """Write each row's outcome against the claim it was taken under.

        A row whose claim was reclaimed while this drainer was sending settles
        nothing and is counted as nothing: another drainer holds it now, and the
        provider discards whichever of the two sends arrives second.
        """

        sent = retried = 0
        abandoned: list[_Outcome] = []
        with self.database.session() as session:
            outbox = BillingMeterOutboxRepository(session)
            for outcome in outcomes:
                event = outcome.event
                if outcome.verdict is _Verdict.Sent:
                    sent += int(
                        outbox.mark_sent(event_id=event.id, claim_token=claim_token, now=now)
                    )
                elif outcome.verdict is _Verdict.Retry:
                    retried += int(
                        outbox.mark_failed(
                            event_id=event.id,
                            claim_token=claim_token,
                            now=now,
                            next_attempt_at=_next_attempt_at(now, event.attempts),
                            error=outcome.error,
                        )
                    )
                elif outbox.abandon(
                    event_id=event.id,
                    claim_token=claim_token,
                    now=now,
                    error=outcome.error,
                ):
                    abandoned.append(outcome)
        # Outside the transaction that settled them: the sink opens its own
        # session, and an event recorded from inside this one would claim a
        # settlement that has not committed.
        for outcome in abandoned:
            self._record_abandonment(outcome)
        return MeterEventDrainResult(
            sent_count=totals.sent_count + sent,
            retried_count=totals.retried_count + retried,
            abandoned_count=totals.abandoned_count + len(abandoned),
        )

    def _record_abandonment(self, outcome: _Outcome) -> None:
        event = outcome.event
        dimension = _DIMENSIONS_BY_METER_EVENT.get(event.meter_event_name)
        try:
            self.events.emit(
                METER_EVENT_ABANDONED_ACTION,
                resource_type=METER_EVENT_RESOURCE_TYPE,
                resource_id=event.identifier,
                message=(
                    f"{event.value_nanos} nanodollars of metered usage was never "
                    f"charged: {outcome.error}"
                ),
                level=EventLevel.Error,
                data={
                    "identifier": event.identifier,
                    "dimension": dimension.value if dimension is not None else "",
                    "meter_event_name": event.meter_event_name,
                    "value_nanos": event.value_nanos,
                    "attempts": event.attempts,
                    "occurred_at": event.occurred_at.isoformat(),
                    "error": outcome.error,
                },
                workspace_id=event.workspace_id,
            )
        except Exception:
            # Wrapped so that failing to record the abandonment cannot replace
            # the abandonment as what this process reports.
            LOGGER.exception(
                "billing: meter event %s was abandoned and the event was not recorded",
                event.identifier,
            )


def _next_attempt_at(now: datetime, attempts: int) -> datetime:
    """When a refused row is offered again.

    Exponential and capped so a provider outage is not hammered, jittered so a
    batch refused together does not come back together.
    """

    doublings = min(max(attempts - 1, 0), _MAX_BACKOFF_DOUBLINGS)
    delay = min(_RETRY_CAP, _RETRY_BASE * 2**doublings)
    return now + delay * (1 + random.uniform(-_RETRY_JITTER, _RETRY_JITTER))


__all__ = [
    "CLAIM_TTL",
    "MAX_ATTEMPTS",
    "METER_EVENT_ABANDONED_ACTION",
    "METER_EVENT_RESOURCE_TYPE",
    "SENT_RETENTION",
    "BillingEventSink",
    "BillingMeterOutboxService",
    "MeterEventDrainResult",
]
