from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.tables.billing_allowance import BillingAllowancePeriodTable
from database.tables.billing_ledger import BillingLedgerSegmentTable
from shared.billing_plans import SubscriptionTermsVersion
from shared.enums import StringEnum
from shared.errors import InvalidInputError, NotFoundError
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

    `Unchanged` is a write that left the row as it stood, which covers a repeat
    of terms already written and a smaller allowance the period declines to take
    — both leave the grant that funds the cycle exactly where it is, so they are
    one answer to the only question the caller asks.
    """

    Unchanged = "unchanged"
    Opened = "opened"
    ReTermed = "re_termed"


@dataclass(frozen=True, slots=True)
class WrittenSubscriptionPeriod:
    """What writing a subscription's cycle did, and what cycle it followed.

    All three answers decide what happens to the grant that funds the cycle, and
    all three are read at the moment the cycle is written, under the lock its
    caller holds.
    """

    outcome: SubscriptionPeriodOutcome
    previous_period_ended_at: datetime | None
    """When the cycle before this one ended, `None` where none precedes it.

    An instant rather than a verdict: a cycle keeps claiming credit for a while
    after it ends, and how long that is belongs to the provider whose invoice is
    doing the claiming. An account's first cycle has nothing behind it, which is
    what makes its allowance spendable the moment it is bought.
    """

    allowance_nanos: int
    """What the period holds now, which is not always what was asked for.

    The figure a grant has to be bought at, because the period row is what the
    customer is shown and a grant sized to anything else would fund a different
    allowance from the one they are reading.
    """


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
    funded_terms_version: SubscriptionTermsVersion | None = None

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
        funded: bool,
    ) -> WrittenSubscriptionPeriod:
        """Make this cycle's terms be these, reporting what that did to them.

        Bounds come from the provider's own cycle rather than a calendar month:
        the included compute is what the subscription carries, so it has to start
        and end when the subscription does or a customer gets two part allowances
        at the seam.

        A cycle already on these terms is left alone, and one whose terms differ
        — a plan changed part-way through — is re-termed in place so the spend
        already counted against it survives.

        An allowance a customer paid for is never reduced inside the period it
        was stamped on. What they were given when the cycle opened is what they
        spent against while it ran, and re-terming it downwards mid-cycle would
        put the smaller figure in front of usage that was included when it
        happened: the credit the provider applies at finalization would fall
        short of the spend the plan had already covered, and the difference would
        be invoiced. So a smaller figure is declined and the larger one kept,
        which is the same rule `BillingAllowanceResponse` states to a customer —
        the allowance is what the period opened on, not what the plan currently
        includes. The end of the cycle is the provider's own and always takes the
        new value.

        `funded` is what separates that from the other reason terms shrink. An
        account nobody can be charged for did not pay for the larger figure: it
        was given on the expectation that somebody could be billed for whatever
        was spent past it, and once that stops being true the platform is not
        holding to it. Attaching a card and removing it again would otherwise
        keep the larger allowance for the rest of the cycle — and every cycle
        after, since each renewal re-terms from a period that still holds it —
        which is the cardless bound removed by the one action a customer can take
        freely. So an unfunded cycle takes the figure it is given, downwards
        included, and a funded one keeps what it opened with.

        The outcome is what the caller pairs with a grant at the provider. One
        delivered renewal arrives as more than one delivery, so the period is
        what settles which of them buys the allowance and which finds it already
        bought — and opening a cycle is a different act from re-terming the one
        in progress, because only the second has an outgoing grant to void.

        The cycle this one follows is reported beside the outcome, because the
        allowance bought here must not be reachable by the invoice that cycle
        raises. These rows are the only record of it: the account row holds the
        newest grant and forgets the one before, and a cycle re-termed part-way
        through has an outgoing grant of its own that says nothing about what
        came earlier.

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
        previous_period_ended_at = self._preceding_period_ended_at(
            user_id=user_id, period_started_at=period_started_at
        )
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
            return WrittenSubscriptionPeriod(
                outcome=SubscriptionPeriodOutcome.Opened,
                previous_period_ended_at=previous_period_ended_at,
                allowance_nanos=allowance_nanos,
            )
        period_id, existing_ended_at, existing_allowance_nanos = existing
        held_nanos = max(existing_allowance_nanos, allowance_nanos) if funded else allowance_nanos
        if (
            to_utc(existing_ended_at) == to_utc(period_ended_at)
            and held_nanos == existing_allowance_nanos
        ):
            return WrittenSubscriptionPeriod(
                outcome=SubscriptionPeriodOutcome.Unchanged,
                previous_period_ended_at=previous_period_ended_at,
                allowance_nanos=existing_allowance_nanos,
            )
        self.session.execute(
            update(BillingAllowancePeriodTable)
            .where(BillingAllowancePeriodTable.id == period_id)
            .values(
                period_ended_at=period_ended_at,
                allowance_nanos=held_nanos,
                updated_at=utc_now(),
            )
        )
        self.session.flush()
        return WrittenSubscriptionPeriod(
            outcome=SubscriptionPeriodOutcome.ReTermed,
            previous_period_ended_at=previous_period_ended_at,
            allowance_nanos=held_nanos,
        )

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
                BillingAllowancePeriodTable.funded_terms_version,
            )
            .where(*_covering(user_id, at))
            .order_by(BillingAllowancePeriodTable.period_started_at.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        period_started_at, period_ended_at, allowance_nanos, spent_nanos, funded_version = row
        return SpentAllowancePeriod(
            started_at=to_utc(period_started_at),
            ended_at=to_utc(period_ended_at),
            allowance_nanos=allowance_nanos,
            spent_nanos=spent_nanos,
            funded_terms_version=SubscriptionTermsVersion(funded_version)
            if funded_version
            else None,
        )

    def record_funded_terms(
        self,
        *,
        user_id: str,
        period_started_at: datetime,
        terms_version: SubscriptionTermsVersion,
    ) -> None:
        row = self.session.scalar(
            select(BillingAllowancePeriodTable)
            .where(
                BillingAllowancePeriodTable.user_id == user_id,
                BillingAllowancePeriodTable.period_started_at == to_utc(period_started_at),
            )
            .with_for_update()
        )
        if row is None:
            raise NotFoundError("the funded subscription period does not exist")
        row.funded_terms_version = terms_version.value
        self.session.flush()

    def confirm_credit(self, *, user_id: str, period_started_at: datetime, at: datetime) -> None:
        self.session.execute(
            update(BillingAllowancePeriodTable)
            .where(
                BillingAllowancePeriodTable.user_id == user_id,
                BillingAllowancePeriodTable.period_started_at == period_started_at,
                BillingAllowancePeriodTable.credit_confirmed_at.is_(None),
            )
            .values(credit_confirmed_at=to_utc(at))
        )
        self.session.flush()

    def unconfirmed_periods(
        self, *, user_id: str, since: datetime, before: datetime
    ) -> tuple[SpentAllowancePeriod, ...]:
        rows = self.session.scalars(
            select(BillingAllowancePeriodTable)
            .where(
                BillingAllowancePeriodTable.user_id == user_id,
                BillingAllowancePeriodTable.period_started_at >= since,
                BillingAllowancePeriodTable.period_started_at < before,
                BillingAllowancePeriodTable.credit_confirmed_at.is_(None),
            )
            .order_by(BillingAllowancePeriodTable.period_started_at)
        ).all()
        return tuple(
            SpentAllowancePeriod(
                to_utc(row.period_started_at),
                to_utc(row.period_ended_at),
                row.allowance_nanos,
                row.spent_nanos,
                SubscriptionTermsVersion(row.funded_terms_version)
                if row.funded_terms_version
                else None,
            )
            for row in rows
        )

    def _preceding_period_ended_at(
        self, *, user_id: str, period_started_at: datetime
    ) -> datetime | None:
        """When the latest cycle beginning before this one ended.

        The latest rather than any, because it is the only one whose invoice can
        still be settling, and cycles meet without overlapping so there is one
        answer. `None` is an account's first cycle, which follows nothing.
        """

        ended_at = self.session.scalars(
            select(BillingAllowancePeriodTable.period_ended_at)
            .where(
                BillingAllowancePeriodTable.user_id == user_id,
                BillingAllowancePeriodTable.period_started_at < period_started_at,
            )
            .order_by(BillingAllowancePeriodTable.period_started_at.desc())
            .limit(1)
        ).first()
        return to_utc(ended_at) if ended_at is not None else None

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
    "WrittenSubscriptionPeriod",
]
