from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_preferences import BillingPreferencesRepository
from database.tables.credit_purchases import CreditPurchaseTable
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.enums import StringEnum
from sqlalchemy.orm import Session


class AutomaticPurchaseDecision(StringEnum):
    Allowed = "allowed"
    Disabled = "disabled"
    Paused = "paused"
    IneligibleAccount = "ineligible_account"


def automatic_reload_account_eligible(account: BillingAccount | None) -> bool:
    return (
        account is not None
        and account.complimentary_since is None
        and account.status is BillingAccountStatus.Active
        and account.payment_method_attached_at is not None
        and account.plan is not None
        and bool(account.provider_subscription_id)
    )


def authorize_automatic_purchase(
    session: Session,
    *,
    purchase: CreditPurchaseTable,
) -> AutomaticPurchaseDecision:
    if not automatic_reload_account_eligible(
        BillingAccountRepository(session).get_by_user(purchase.user_id)
    ):
        return AutomaticPurchaseDecision.IneligibleAccount
    repository = BillingPreferencesRepository(session)
    preferences = repository.get(purchase.user_id)
    if not preferences.reload_enabled:
        return AutomaticPurchaseDecision.Disabled
    if repository.reload_state(purchase.user_id).paused_purchase_id is not None:
        return AutomaticPurchaseDecision.Paused
    return AutomaticPurchaseDecision.Allowed


__all__ = [
    "AutomaticPurchaseDecision",
    "authorize_automatic_purchase",
    "automatic_reload_account_eligible",
]
