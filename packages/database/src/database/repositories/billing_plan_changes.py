from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.tables.billing_plan_changes import BillingPlanChangeIntentTable
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.errors import ConflictError
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

_ERROR_LIMIT = 512

_OPEN_STATUSES = ("pending", "settling")
"""An intent nobody has recorded an outcome for, waiting or in flight.

The same set answers the operator's count and the customer's own question, and
one of them reading a status the other did not would be a change visible to
exactly one of them.
"""


@dataclass(frozen=True, slots=True)
class ClaimedPlanChange:
    """One intent a settler holds the claim on.

    Everything settling needs is here: who pays, the subscription to ask about,
    and the plan the answer is compared against. Nothing is re-resolved from the
    account row, so an intent settles against what was attempted rather than
    against whatever the row says by the time somebody gets to it.
    """

    id: str
    user_id: str
    provider_customer_id: str
    provider_subscription_id: str
    target_plan: BillingPlanId
    target_terms_version: SubscriptionTermsVersion
    created_at: datetime
    attempts: int


@dataclass(frozen=True, slots=True)
class BillingPlanChangeIntentRepository:
    session: Session

    def open(
        self,
        *,
        user_id: str,
        provider_customer_id: str,
        provider_subscription_id: str,
        target_plan: BillingPlanId,
        target_terms_version: SubscriptionTermsVersion,
        now: datetime,
        claim_token: str,
    ) -> ClaimedPlanChange:
        """Record a plan change before it is attempted, claimed by its author.

        Inserted already claimed, so the sweep passes over it until the claim
        goes stale: the request that opened it is its first settler, and a
        second one taking it while the provider call is still in flight would
        read a subscription that has not moved yet.

        The partial unique index refuses a second open intent for one account,
        which is what serializes two simultaneous subscribes now that the
        request no longer holds one transaction across the provider call.
        """

        row = BillingPlanChangeIntentTable(
            id=str(uuid4()),
            user_id=user_id,
            provider_customer_id=provider_customer_id,
            provider_subscription_id=provider_subscription_id,
            target_plan=target_plan.value,
            target_terms_version=target_terms_version.value,
            status="settling",
            attempts=1,
            next_attempt_at=now,
            claimed_at=now,
            claim_token=claim_token,
            last_error="",
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            # Named for the person reading it: this message is the body of the
            # 409 the dashboard shows, and the account it is about is theirs.
            raise ConflictError("a plan change for this account is already being settled") from exc
        return ClaimedPlanChange(
            id=row.id,
            user_id=row.user_id,
            provider_customer_id=row.provider_customer_id,
            provider_subscription_id=row.provider_subscription_id,
            target_plan=target_plan,
            target_terms_version=target_terms_version,
            created_at=row.created_at,
            attempts=row.attempts,
        )

    def claim(
        self, *, now: datetime, limit: int, claim_token: str
    ) -> tuple[ClaimedPlanChange, ...]:
        """Take up to `limit` due intents for this settler alone.

        `FOR UPDATE SKIP LOCKED` for the reason the outbox uses it: an intent
        another settler holds is passed over rather than waited on. The attempt
        is spent at claim, so a settler that dies mid-provider-call still burns
        one and cannot spin on the same intent forever.
        """

        if limit <= 0:
            return ()
        due = (
            select(BillingPlanChangeIntentTable.id)
            .where(
                BillingPlanChangeIntentTable.status == "pending",
                BillingPlanChangeIntentTable.next_attempt_at <= now,
            )
            .order_by(
                BillingPlanChangeIntentTable.next_attempt_at,
                BillingPlanChangeIntentTable.created_at,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed = self.session.execute(
            update(BillingPlanChangeIntentTable)
            .where(BillingPlanChangeIntentTable.id.in_(due.scalar_subquery()))
            .values(
                status="settling",
                claim_token=claim_token,
                claimed_at=now,
                attempts=BillingPlanChangeIntentTable.attempts + 1,
                updated_at=now,
            )
            .returning(
                BillingPlanChangeIntentTable.id,
                BillingPlanChangeIntentTable.user_id,
                BillingPlanChangeIntentTable.provider_customer_id,
                BillingPlanChangeIntentTable.provider_subscription_id,
                BillingPlanChangeIntentTable.target_plan,
                BillingPlanChangeIntentTable.target_terms_version,
                BillingPlanChangeIntentTable.created_at,
                BillingPlanChangeIntentTable.attempts,
            )
            .execution_options(synchronize_session=False)
        ).all()
        return tuple(
            ClaimedPlanChange(
                id=row.id,
                user_id=row.user_id,
                provider_customer_id=row.provider_customer_id,
                provider_subscription_id=row.provider_subscription_id,
                target_plan=BillingPlanId(row.target_plan),
                target_terms_version=SubscriptionTermsVersion(row.target_terms_version),
                created_at=row.created_at,
                attempts=row.attempts,
            )
            for row in claimed
        )

    def reclaim(self, *, now: datetime, claimed_before: datetime) -> int:
        """Return intents whose settler went away to the queue.

        Safe to hand to somebody else because settling reads the subscription
        back and writes what it says: two settlers reach the same verdict, and
        the account row lock they both take makes the second's writes a no-op.
        """

        result = self.session.execute(
            update(BillingPlanChangeIntentTable)
            .where(
                BillingPlanChangeIntentTable.status == "settling",
                BillingPlanChangeIntentTable.claimed_at < claimed_before,
            )
            .values(status="pending", claim_token=None, claimed_at=None, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        self.session.flush()
        return _rowcount(result)

    def mark_applied(self, *, intent_id: str, claim_token: str, now: datetime) -> bool:
        """The provider holds the plan asked for; false where the claim was lost."""

        return self._settle(
            intent_id=intent_id,
            claim_token=claim_token,
            now=now,
            status="applied",
            last_error="",
        )

    def mark_not_applied(self, *, intent_id: str, claim_token: str, now: datetime) -> bool:
        """The provider never moved, so nothing was charged and nothing is owed."""

        return self._settle(
            intent_id=intent_id,
            claim_token=claim_token,
            now=now,
            status="not_applied",
            last_error="",
        )

    def mark_failed(
        self,
        *,
        intent_id: str,
        claim_token: str,
        now: datetime,
        next_attempt_at: datetime,
        error: str,
    ) -> bool:
        """Return an intent to the queue at a time the caller paced."""

        return self._settle(
            intent_id=intent_id,
            claim_token=claim_token,
            now=now,
            status="pending",
            last_error=error,
            next_attempt_at=next_attempt_at,
        )

    def abandon(self, *, intent_id: str, claim_token: str, now: datetime, error: str) -> bool:
        """Stop asking, keeping the row as the evidence of an unresolved change."""

        return self._settle(
            intent_id=intent_id,
            claim_token=claim_token,
            now=now,
            status="abandoned",
            last_error=error,
        )

    def open_count(self) -> int:
        """Intents with no outcome yet, whether waiting or in flight."""

        return int(
            self.session.scalar(
                select(func.count(BillingPlanChangeIntentTable.id)).where(
                    BillingPlanChangeIntentTable.status.in_(_OPEN_STATUSES)
                )
            )
            or 0
        )

    def has_open(self, *, user_id: str) -> bool:
        """Whether a change for this account is still waiting on an outcome.

        What a customer is shown while their own change is being settled. The
        partial unique index means there is at most one, and a retry schedule
        spanning hours means the wait is long enough to need saying: an account
        that is offered the same button meanwhile is offered a conflict.
        """

        return (
            self.session.scalars(
                select(BillingPlanChangeIntentTable.id)
                .where(
                    BillingPlanChangeIntentTable.user_id == user_id,
                    BillingPlanChangeIntentTable.status.in_(_OPEN_STATUSES),
                )
                .limit(1)
            ).first()
            is not None
        )

    def _settle(
        self,
        *,
        intent_id: str,
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
            update(BillingPlanChangeIntentTable)
            .where(
                BillingPlanChangeIntentTable.id == intent_id,
                BillingPlanChangeIntentTable.claim_token == claim_token,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        self.session.flush()
        return _rowcount(result) == 1


def _rowcount(result: object) -> int:
    return int(result.rowcount) if isinstance(result, CursorResult) else 0


__all__ = ["BillingPlanChangeIntentRepository", "ClaimedPlanChange"]
