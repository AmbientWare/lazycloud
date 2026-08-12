from __future__ import annotations

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_periods import BillingPeriodRepository
from shared.billing_accounts import BillingAccountStatus
from shared.billing_periods import BillingPeriodStatus
from sqlalchemy.orm import Session


def reconcile_standing(session: Session, *, user_id: str) -> None:
    """Set an account's standing from every month it still owes on.

    Derived rather than moved, and shared by both things that learn an outcome:
    the charge at close, which knows immediately, and the webhook, which learns
    the same thing again later or learns of a payment made elsewhere. Two callers
    each nudging the status would disagree the moment they ran in the wrong
    order — and being retried for days, deliveries do.

    An account with a failed September and a paid October is behind. That is a
    question about the set of months, not about whichever outcome arrived last,
    which is why it is recomputed here instead of being written at the point the
    outcome was found.

    Nothing to do for an account with no row: absence is the free plan, and a
    free account has nothing to be behind on. The plan is read and written back
    untouched, because a plan changes when someone chooses and standing changes
    when a payment succeeds or fails.
    """

    accounts = BillingAccountRepository(session)
    account = accounts.get_by_user(user_id, for_update=True)
    if account is None:
        return
    behind = any(
        period.status is BillingPeriodStatus.PaymentFailed
        for period in BillingPeriodRepository(session).outstanding_for(user_id=user_id)
    )
    status = BillingAccountStatus.PastDue if behind else BillingAccountStatus.Active
    if account.status is status:
        return
    accounts.upsert(
        user_id=account.user_id,
        plan=account.plan,
        status=status,
        provider_customer_id=account.provider_customer_id,
    )


__all__ = ["reconcile_standing"]
