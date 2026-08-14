from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.tables.billing_allowance import BillingAllowancePeriodTable
from database.tables.billing_ledger import BillingLedgerSegmentTable
from shared.enums import StringEnum
from shared.errors import InvalidInputError
from shared.timestamps import to_utc, utc_now
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session


class SubscriptionPeriodOutcome(StringEnum):
    """What writing a subscription's cycle did to the terms already held.

    The three are decided apart because the grant that funds a cycle is bought
    per cycle and expiring one is irreversible. A cycle opened for the first time
    leaves the previous cycle's grant alone — that grant is what funds the
    invoice finalizing at that moment. A cycle re-termed in place is one plan
    swapped for another inside it, and its outgoing grant has to go or the
    customer holds two allowances for one cycle.
    """

    Unchanged = "unchanged"
    Opened = "opened"
    ReTermed = "re_termed"


@dataclass(frozen=True, slots=True)
class SpentAllowancePeriod:
    """One stretch of time, the terms that hold over it, and what they have cost.

    Half-open `[started_at, ended_at)`, so consecutive periods meet without
    sharing an instant and a cost belongs to exactly one of them.
    """

    started_at: datetime
    ended_at: datetime
    allowance_nanos: int
    spent_nanos: int

    @property
    def remaining_nanos(self) -> int:
        """Signed: negative once the allowance is overspent.

        Overspending is normal — the overage is billed rather than refused — so
        how far past the line an account is has to survive the read.
        """

        return self.allowance_nanos - self.spent_nanos


@dataclass(frozen=True, slots=True)
class BillingAllowanceRepository:
    """What an account has spent against the terms its period came with.

    A local counter rather than a balance read from the provider, because the
    dashboard shows it on every load and the provider's own copy is an invoice
    that does not exist until the cycle closes.
    """

    session: Session

    def set_subscription_period(
        self,
        *,
        user_id: str,
        period_started_at: datetime,
        period_ended_at: datetime,
        allowance_nanos: int,
    ) -> SubscriptionPeriodOutcome:
        """Make this cycle's terms be these, reporting what that did to them.

        Bounds come from the provider's own cycle rather than a calendar month:
        the included compute is what the subscription carries, so it has to start
        and end when the subscription does or a customer gets two part allowances
        at the seam.

        A cycle already on these terms is left alone, and one whose terms differ
        — a plan changed part-way through — is re-termed in place so the spend
        already counted against it survives.

        The outcome is what the caller pairs with a grant at the provider. One
        delivered renewal arrives as more than one delivery, so the period is
        what settles which of them buys the allowance and which finds it already
        bought — and opening a cycle is a different act from re-terming the one
        in progress, because only the second has an outgoing grant to void.

        A cycle opened for the first time starts with whatever the ledger already
        priced inside it. Cost is priced on its own schedule and the cycle is
        opened by a delivery, so usage between a cycle beginning at the provider
        and this row existing has nowhere to be counted at the moment it is
        priced; reading it back from the ledger here is what stops that spend
        from being lost from the figure the customer is shown.

        Concurrent callers are serialized by the account row lock each of them
        takes first, which is also what makes the read-then-write below safe.
        """

        if period_ended_at <= period_started_at:
            raise InvalidInputError("an allowance period must end after it starts")
        if allowance_nanos < 0:
            raise InvalidInputError("an allowance is never negative")
        existing = self.session.execute(
            select(
                BillingAllowancePeriodTable.id,
                BillingAllowancePeriodTable.period_ended_at,
                BillingAllowancePeriodTable.allowance_nanos,
            ).where(
                BillingAllowancePeriodTable.user_id == user_id,
                BillingAllowancePeriodTable.period_started_at == period_started_at,
            )
        ).first()
        if existing is None:
            self.session.add(
                BillingAllowancePeriodTable(
                    id=str(uuid4()),
                    user_id=user_id,
                    period_started_at=period_started_at,
                    period_ended_at=period_ended_at,
                    allowance_nanos=allowance_nanos,
                    spent_nanos=self._priced_within(
                        user_id=user_id,
                        started_at=period_started_at,
                        ended_at=period_ended_at,
                    ),
                )
            )
            self.session.flush()
            return SubscriptionPeriodOutcome.Opened
        period_id, existing_ended_at, existing_allowance_nanos = existing
        if (
            to_utc(existing_ended_at) == to_utc(period_ended_at)
            and existing_allowance_nanos == allowance_nanos
        ):
            return SubscriptionPeriodOutcome.Unchanged
        self.session.execute(
            update(BillingAllowancePeriodTable)
            .where(BillingAllowancePeriodTable.id == period_id)
            .values(
                period_ended_at=period_ended_at,
                allowance_nanos=allowance_nanos,
                updated_at=utc_now(),
            )
        )
        self.session.flush()
        return SubscriptionPeriodOutcome.ReTermed

    def increment(self, *, user_id: str, at: datetime, cost_nanos: int) -> None:
        """Add a cost to the period that covers `at`, if one does.

        Where periods overlap the most recently begun one takes the cost, so the
        answer never depends on row order.

        A cost falling outside every period is one priced in the gap between a
        cycle ending at the provider and the delivery that opens the next here.
        It is not counted against terms that do not exist yet, and it is not lost
        either: the ledger row it was written beside is what
        `set_subscription_period` reads when it opens that cycle, so the spend
        lands on the period that covers it as soon as there is one.
        """

        if cost_nanos < 0:
            raise InvalidInputError("an allowance is spent, never refunded, by pricing")
        period_id = self.session.scalars(
            select(BillingAllowancePeriodTable.id)
            .where(*_covering(user_id, at))
            .order_by(BillingAllowancePeriodTable.period_started_at.desc())
            .limit(1)
        ).first()
        if period_id is None:
            return
        self.session.execute(
            update(BillingAllowancePeriodTable)
            .where(BillingAllowancePeriodTable.id == period_id)
            .values(
                spent_nanos=BillingAllowancePeriodTable.spent_nanos + cost_nanos,
                updated_at=utc_now(),
            )
        )
        self.session.flush()

    def current_period(self, *, user_id: str, at: datetime) -> SpentAllowancePeriod | None:
        """The period covering `at`, and what has been spent against it.

        `None` where no period covers the instant, which is an account nothing
        has granted terms to. Inventing terms to answer with would be showing a
        customer an allowance nothing will hold them to.
        """

        row = self.session.execute(
            select(
                BillingAllowancePeriodTable.period_started_at,
                BillingAllowancePeriodTable.period_ended_at,
                BillingAllowancePeriodTable.allowance_nanos,
                BillingAllowancePeriodTable.spent_nanos,
            )
            .where(*_covering(user_id, at))
            .order_by(BillingAllowancePeriodTable.period_started_at.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        period_started_at, period_ended_at, allowance_nanos, spent_nanos = row
        return SpentAllowancePeriod(
            started_at=to_utc(period_started_at),
            ended_at=to_utc(period_ended_at),
            allowance_nanos=allowance_nanos,
            spent_nanos=spent_nanos,
        )

    def _priced_within(self, *, user_id: str, started_at: datetime, ended_at: datetime) -> int:
        """What the ledger already holds for this payer inside a cycle's bounds.

        Read over `segment_started_at`, which is the same instant `increment` is
        given, so a segment is counted here exactly when it would have been
        counted there. Indexed by `(owner_user_id, segment_started_at)`.
        """

        return int(
            self.session.scalar(
                select(func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0)).where(
                    BillingLedgerSegmentTable.owner_user_id == user_id,
                    BillingLedgerSegmentTable.segment_started_at >= started_at,
                    BillingLedgerSegmentTable.segment_started_at < ended_at,
                )
            )
            or 0
        )


def _covering(user_id: str, at: datetime):
    return (
        BillingAllowancePeriodTable.user_id == user_id,
        BillingAllowancePeriodTable.period_started_at <= at,
        BillingAllowancePeriodTable.period_ended_at > at,
    )


__all__ = [
    "BillingAllowanceRepository",
    "SpentAllowancePeriod",
    "SubscriptionPeriodOutcome",
]
