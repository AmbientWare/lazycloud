from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from database.tables.billing_outbox import BillingMeterOutboxTable
from shared.timestamps import to_utc
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.orm import Session

_ERROR_LIMIT = 512


@dataclass(frozen=True, slots=True)
class ClaimedMeterEvent:
    """One row a drainer holds the claim on.

    Everything the send needs is here: the customer, the meter, the value and the
    identifier the provider deduplicates on. Nothing is re-resolved at send time,
    so a restart resumes by claiming and a database restore resumes from whatever
    committed.
    """

    id: str
    workspace_id: str
    identifier: str
    provider_customer_id: str
    meter_event_name: str
    value_nanos: int
    pricing_version: str
    occurred_at: datetime
    attempts: int


@dataclass(frozen=True, slots=True)
class UndeliveredMeterTotals:
    """One meter's value that has not reached the provider, by whether it can.

    Nanodollars, as the rows themselves count: `waiting_nanos` is queued or in
    flight and will be offered again, `abandoned_nanos` has been given up on and
    will not, and `waived_nanos` was never owed because an administrator waived
    the account's bill while it was priced.
    """

    waiting_nanos: int = 0
    abandoned_nanos: int = 0
    waived_nanos: int = 0


@dataclass(frozen=True, slots=True)
class BillingMeterOutboxRepository:
    session: Session

    def claim(
        self,
        *,
        now: datetime,
        limit: int,
        claim_token: str,
    ) -> tuple[ClaimedMeterEvent, ...]:
        """Take up to `limit` due rows for this drainer alone.

        `FOR UPDATE SKIP LOCKED` is what makes several drainers safe without a
        lease table or a lock in Redis: PostgreSQL already owns this state, and a
        row another drainer holds is passed over rather than waited on.

        The attempt is spent at claim rather than at failure, so a drainer that
        dies mid-send still burns one and cannot spin on the same row forever.
        """

        if limit <= 0:
            return ()
        due = (
            select(BillingMeterOutboxTable.id)
            .where(
                BillingMeterOutboxTable.status == "pending",
                BillingMeterOutboxTable.next_attempt_at <= now,
            )
            .order_by(
                BillingMeterOutboxTable.next_attempt_at,
                BillingMeterOutboxTable.created_at,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed = self.session.execute(
            update(BillingMeterOutboxTable)
            .where(BillingMeterOutboxTable.id.in_(due.scalar_subquery()))
            .values(
                status="sending",
                claim_token=claim_token,
                claimed_at=now,
                attempts=BillingMeterOutboxTable.attempts + 1,
                updated_at=now,
            )
            .returning(
                BillingMeterOutboxTable.id,
                BillingMeterOutboxTable.workspace_id,
                BillingMeterOutboxTable.identifier,
                BillingMeterOutboxTable.provider_customer_id,
                BillingMeterOutboxTable.meter_event_name,
                BillingMeterOutboxTable.value_nanos,
                BillingMeterOutboxTable.pricing_version,
                BillingMeterOutboxTable.occurred_at,
                BillingMeterOutboxTable.attempts,
            )
            .execution_options(synchronize_session=False)
        ).all()
        return tuple(
            ClaimedMeterEvent(
                id=row.id,
                workspace_id=row.workspace_id,
                identifier=row.identifier,
                provider_customer_id=row.provider_customer_id,
                meter_event_name=row.meter_event_name,
                value_nanos=row.value_nanos,
                pricing_version=row.pricing_version,
                occurred_at=to_utc(row.occurred_at),
                attempts=row.attempts,
            )
            for row in claimed
        )

    def mark_sent(self, *, event_id: str, claim_token: str, now: datetime) -> bool:
        """Settle a row this drainer still holds; false where it lost the claim."""

        return self._settle(
            event_id=event_id,
            claim_token=claim_token,
            now=now,
            status="sent",
            last_error="",
        )

    def mark_failed(
        self,
        *,
        event_id: str,
        claim_token: str,
        now: datetime,
        next_attempt_at: datetime,
        error: str,
    ) -> bool:
        """Return a row to the queue at a time the caller paced."""

        return self._settle(
            event_id=event_id,
            claim_token=claim_token,
            now=now,
            status="pending",
            last_error=error,
            next_attempt_at=next_attempt_at,
        )

    def abandon(self, *, event_id: str, claim_token: str, now: datetime, error: str) -> bool:
        """Stop trying, keeping the row as the evidence of money that never left."""

        return self._settle(
            event_id=event_id,
            claim_token=claim_token,
            now=now,
            status="abandoned",
            last_error=error,
        )

    def reclaim(self, *, now: datetime, claimed_before: datetime) -> int:
        """Return rows whose drainer went away to the queue.

        A resend after a lost acknowledgement is harmless because the provider
        deduplicates on `identifier`, which is what makes at-least-once the right
        guarantee here rather than a compromise.
        """

        result = self.session.execute(
            update(BillingMeterOutboxTable)
            .where(
                BillingMeterOutboxTable.status == "sending",
                BillingMeterOutboxTable.claimed_at < claimed_before,
            )
            .values(status="pending", claim_token=None, claimed_at=None, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        self.session.flush()
        return _rowcount(result)

    def abandoned_total(self) -> tuple[int, int]:
        """How many charges have been given up on, and what they metered.

        The value is what the usage was priced at rather than what it would have
        been billed: an account spends its allowance before it is charged for
        anything, so part of any total here would have reached an invoice at
        zero.

        A standing figure rather than a delta, because a row is abandoned once
        and pruning never removes one: the count only falls when somebody has
        answered for the money and deleted the evidence. Served by
        `ix_billing_meter_outbox_settled`.
        """

        row = self.session.execute(
            select(
                func.count(BillingMeterOutboxTable.id),
                func.coalesce(func.sum(BillingMeterOutboxTable.value_nanos), 0),
            ).where(BillingMeterOutboxTable.status == "abandoned")
        ).one()
        return int(row[0]), int(row[1])

    def undelivered_totals(
        self, *, provider_customer_id: str, started_at: datetime, ended_at: datetime
    ) -> Mapping[str, UndeliveredMeterTotals]:
        """What this customer's window never reached the provider with, per meter.

        Split by whether it still can. None of it is on the bill, so all of it
        comes off the ledger before an invoice is compared against it. They are
        different facts even so: a delivery that has not happened yet, one that
        never will, and one that was never owed. Collapsing them would let a
        charge nobody will ever make read as an account in perfect agreement.
        """

        found = self.session.execute(
            select(
                BillingMeterOutboxTable.meter_event_name,
                BillingMeterOutboxTable.status,
                func.coalesce(func.sum(BillingMeterOutboxTable.value_nanos), 0),
            )
            .where(
                BillingMeterOutboxTable.provider_customer_id == provider_customer_id,
                BillingMeterOutboxTable.occurred_at >= started_at,
                BillingMeterOutboxTable.occurred_at < ended_at,
                BillingMeterOutboxTable.status != "sent",
            )
            .group_by(
                BillingMeterOutboxTable.meter_event_name,
                BillingMeterOutboxTable.status,
            )
        ).all()
        totals: dict[str, UndeliveredMeterTotals] = {}
        for meter_event_name, status, value_nanos in found:
            held = totals.get(str(meter_event_name), UndeliveredMeterTotals())
            nanos = int(value_nanos)
            if status == "abandoned":
                held = replace(held, abandoned_nanos=held.abandoned_nanos + nanos)
            elif status == "waived":
                held = replace(held, waived_nanos=held.waived_nanos + nanos)
            else:
                held = replace(held, waiting_nanos=held.waiting_nanos + nanos)
            totals[str(meter_event_name)] = held
        return totals

    def prune(self, *, sent_before: datetime, limit: int) -> int:
        """Delete settled rows in a bounded batch.

        Only `sent`. An `abandoned` row is the record of a charge that never
        reached the provider and is kept until somebody has answered for it.
        """

        if limit <= 0:
            return 0
        settled = (
            select(BillingMeterOutboxTable.id)
            .where(
                BillingMeterOutboxTable.status == "sent",
                BillingMeterOutboxTable.updated_at < sent_before,
            )
            .order_by(BillingMeterOutboxTable.updated_at)
            .limit(limit)
        )
        result = self.session.execute(
            delete(BillingMeterOutboxTable)
            .where(BillingMeterOutboxTable.id.in_(settled.scalar_subquery()))
            .execution_options(synchronize_session=False)
        )
        self.session.flush()
        return _rowcount(result)

    def _settle(
        self,
        *,
        event_id: str,
        claim_token: str,
        now: datetime,
        status: str,
        last_error: str,
        next_attempt_at: datetime | None = None,
    ) -> bool:
        values: dict[str, str | datetime | None] = {
            "status": status,
            "claim_token": None,
            "claimed_at": None,
            "last_error": last_error[:_ERROR_LIMIT],
            "updated_at": now,
        }
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        result = self.session.execute(
            update(BillingMeterOutboxTable)
            .where(
                BillingMeterOutboxTable.id == event_id,
                BillingMeterOutboxTable.claim_token == claim_token,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        self.session.flush()
        return _rowcount(result) == 1


def _rowcount(result: object) -> int:
    return int(result.rowcount) if isinstance(result, CursorResult) else 0


__all__ = ["BillingMeterOutboxRepository", "ClaimedMeterEvent", "UndeliveredMeterTotals"]
