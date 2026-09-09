from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.tables.billing_allowance import BillingAllowancePeriodTable
from database.tables.billing_ledger import BillingLedgerSegmentTable
from shared.billing_plans import SubscriptionTermsVersion
from shared.errors import InvalidInputError, NotFoundError
from shared.timestamps import to_utc, utc_now
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session


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
    ) -> None:
        """Record provider cycle bounds under the caller's billing account lock.

        Backfill spend from the ledger when usage precedes the renewal webhook.
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
            return
        period_id, existing_ended_at, existing_allowance_nanos = existing
        if (
            to_utc(existing_ended_at) == to_utc(period_ended_at)
            and allowance_nanos == existing_allowance_nanos
        ):
            return
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
]
