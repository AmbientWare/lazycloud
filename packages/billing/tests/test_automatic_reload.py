from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from api.server.services import ApiServices
from billing.automatic_reload import AutomaticReloadService
from billing.automatic_reload_policy import AutomaticPurchaseDecision, authorize_automatic_purchase
from billing.preferences import BillingPreferencesService
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_preferences import BillingPreferencesRepository
from database.repositories.credit_purchases import CreditPurchaseRepository
from shared.billing_credits import CreditGrant, CreditKind
from shared.billing_preferences import AutomaticReloadPauseReason
from shared.credit_payments import CreditPaymentStatus
from shared.http.billing_preferences import BillingPreferences
from shared.timestamps import utc_now
from tests.workspaces import workspace_owner_user_id


def test_reload_serializes_payments_preserves_pause_and_continues_after_refunds(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    preferences = BillingPreferences(reload_enabled=True)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
        BillingAccountRepository(session).set_payment_method_present(
            user_id=user_id, present=True, at=now
        )
        credits = BillingCreditRepository(session)
        BillingPreferencesService(session).set(user_id=user_id, preferences=preferences)
    service = AutomaticReloadService(
        isolated_services.context.database,
        isolated_services.payment_provider,
    )
    barrier = Barrier(2)

    def prepare() -> str | None:
        barrier.wait(timeout=5)
        return service.prepare_due_reload(user_id=user_id, now=now)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(prepare) for _ in range(2)]
        prepared = [result.result(timeout=15) for result in results]
    assert sum(item is not None for item in prepared) == 1
    purchase_id = next(item for item in prepared if item is not None)
    assert service.status(user_id=user_id, now=now).monthly_payment_committed_cents == 2000

    with isolated_services.context.database.session() as session:
        BillingPreferencesService(session).set(
            user_id=user_id,
            preferences=preferences.model_copy(update={"reload_enabled": False}),
        )
        purchase = CreditPurchaseRepository(session).get(purchase_id=purchase_id, user_id=user_id)
        assert purchase is not None
        assert (
            authorize_automatic_purchase(
                session,
                purchase=purchase,
            )
            is AutomaticPurchaseDecision.Disabled
        )
        purchase.status = CreditPaymentStatus.Declined.value
    status = service.status(user_id=user_id)
    assert status.pause_reason is AutomaticReloadPauseReason.Declined
    with isolated_services.context.database.session() as session:
        assert (
            BillingPreferencesRepository(session).reload_state(user_id).paused_purchase_id is None
        )
    assert service.prepare_due_reload(user_id=user_id, now=now) is None
    with isolated_services.context.database.session() as session:
        BillingPreferencesService(session).set(user_id=user_id, preferences=preferences)
    assert service.prepare_due_reload(user_id=user_id, now=now) is None
    assert service.status(user_id=user_id).paused_purchase_id == status.paused_purchase_id
    assert service.resume(user_id=user_id).paused_purchase_id is None
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).set_complimentary(user_id=user_id, present=True, at=now)
    assert service.prepare_due_reload(user_id=user_id, now=now) is None
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).set_complimentary(user_id=user_id, present=False, at=now)
    next_id = service.prepare_due_reload(user_id=user_id, now=utc_now())
    assert next_id is not None

    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).get_by_user(user_id, for_update=True)
        purchase = CreditPurchaseRepository(session).get(purchase_id=next_id, user_id=user_id)
        assert purchase is not None
        credits = BillingCreditRepository(session)
        lot_id = credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:confirmed-reload",
                CreditKind.Purchased,
                purchase.amount_nanos,
                now,
            ),
        )
        purchase.credit_lot_id = lot_id
        purchase.funded_at = now
        purchase.status = CreditPaymentStatus.Succeeded.value
        purchase.reversed_nanos = purchase.amount_nanos
        credits.adjust(
            user_id=user_id,
            credit_lot_id=lot_id,
            source_id="refund:confirmed-reload",
            amount_nanos=-purchase.amount_nanos,
            effective_at=now,
        )
    assert service.status(user_id=user_id, now=now).monthly_payment_committed_cents == 2000
    pending_id = service.prepare_due_reload(user_id=user_id, now=utc_now())
    assert pending_id is not None
    with isolated_services.context.database.session() as session:
        purchase = CreditPurchaseRepository(session).get(purchase_id=pending_id, user_id=user_id)
        assert purchase is not None
        assert (
            authorize_automatic_purchase(
                session,
                purchase=purchase,
            )
            is AutomaticPurchaseDecision.Allowed
        )
        BillingAccountRepository(session).set_payment_method_present(
            user_id=user_id, present=False, at=now
        )
        assert (
            authorize_automatic_purchase(session, purchase=purchase)
            is AutomaticPurchaseDecision.IneligibleAccount
        )
        purchase.status = CreditPaymentStatus.Cancelled.value
    assert service.prepare_due_reload(user_id=user_id, now=utc_now()) is None
