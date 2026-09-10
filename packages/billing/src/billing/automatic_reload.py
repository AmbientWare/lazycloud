import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from database.client import DatabaseClient
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_preferences import BillingPreferencesRepository
from database.repositories.credit_purchases import CreditPurchaseRepository
from shared.billing_preferences import AutomaticReloadPauseReason, usage_budget_month
from shared.billing_quotes import NANOS_PER_USD
from shared.credit_payments import CreditPaymentStatus, CreditPurchaseKind
from shared.errors import ConflictError, DomainError, NotFoundError
from shared.http.billing_preferences import AutomaticReloadStatus
from shared.payments import CreditPurchasePaymentProvider
from shared.timestamps import to_utc, utc_now
from sqlalchemy.orm import Session

from billing.automatic_reload_policy import automatic_reload_account_eligible
from billing.purchases import CreditPurchaseService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AutomaticReloadResult:
    accounts_checked: int = 0
    purchases_started: int = 0
    failed_count: int = 0


@dataclass(frozen=True, slots=True)
class AutomaticReloadService:
    database: DatabaseClient
    payments: Callable[[], CreditPurchasePaymentProvider]

    def status(self, *, user_id: str, now: datetime | None = None) -> AutomaticReloadStatus:
        start, end = usage_budget_month(now or utc_now())
        with self.database.session() as session:
            if BillingAccountRepository(session).get_by_user(user_id) is None:
                raise NotFoundError("the billing account does not exist")
            state = BillingPreferencesRepository(session).reload_state(user_id)
            purchases = CreditPurchaseRepository(session)
            failure = purchases.latest_automatic_failure(user_id=user_id, since=state.resumed_at)
            paused_id = state.paused_purchase_id or (failure.id if failure is not None else None)
            pause_reason = state.pause_reason or (
                AutomaticReloadPauseReason(failure.status) if failure is not None else None
            )
            pending = purchases.pending_automatic(user_id=user_id)
            committed = purchases.automatic_payment_commitment(
                user_id=user_id, start=start, end=end
            )
            return AutomaticReloadStatus(
                paused_purchase_id=UUID(paused_id) if paused_id else None,
                pause_reason=pause_reason,
                pending_purchase_id=UUID(pending.id) if pending is not None else None,
                month_started_at=start,
                month_ended_at=end,
                monthly_payment_committed_cents=committed // (NANOS_PER_USD // 100),
            )

    def resume(self, *, user_id: str) -> AutomaticReloadStatus:
        with self.database.session() as session:
            self._account(session, user_id)
            self._pause_failed_purchase(session, user_id)
            paused_id = (
                BillingPreferencesRepository(session).reload_state(user_id).paused_purchase_id
            )
            pending = CreditPurchaseRepository(session).pending_automatic(user_id=user_id)
            pending_id = pending.id if pending is not None else None
        if paused_id is None:
            return self.status(user_id=user_id)
        if pending_id is not None:
            outcome = CreditPurchaseService(self.database, self.payments).cancel_automatic(
                user_id=user_id,
                purchase_id=pending_id,
            )
            if outcome.status in (CreditPaymentStatus.Pending, CreditPaymentStatus.ActionRequired):
                raise ConflictError("the previous automatic payment still needs reconciliation")
        with self.database.session() as session:
            self._account(session, user_id)
            repository = BillingPreferencesRepository(session)
            if repository.reload_state(user_id).paused_purchase_id != paused_id:
                raise ConflictError(
                    "automatic reload pause changed while the payment was reconciled"
                )
            if CreditPurchaseRepository(session).pending_automatic(user_id=user_id) is not None:
                raise ConflictError("another automatic payment is still unresolved")
            repository.resume_reload(user_id=user_id, at=utc_now())
        return self.status(user_id=user_id)

    def sweep(self, *, limit: int = 20, now: datetime | None = None) -> AutomaticReloadResult:
        moment = to_utc(now or utc_now())
        with self.database.session() as session:
            due = BillingPreferencesRepository(session).due_reloads(at=moment, limit=limit)
        started = failed = 0
        for user_id in due:
            try:
                purchase_id = self.prepare_due_reload(user_id=user_id, now=moment)
                if purchase_id is None:
                    continue
                started += 1
                CreditPurchaseService(self.database, self.payments).reconcile(
                    user_id=user_id,
                    purchase_id=purchase_id,
                )
                with self.database.session() as session:
                    self._account(session, user_id)
                    self._pause_failed_purchase(session, user_id)
            except DomainError as exc:
                failed += 1
                LOGGER.warning("automatic reload failed for account %s: %s", user_id, exc.code)
        return AutomaticReloadResult(len(due), started, failed)

    def prepare_due_reload(self, *, user_id: str, now: datetime) -> str | None:
        with self.database.session() as session:
            account = BillingAccountRepository(session).get_by_user(user_id, for_update=True)
            if account is None:
                raise NotFoundError("the billing account does not exist")
            repository = BillingPreferencesRepository(session)
            preferences = repository.get(user_id)
            repository.mark_reload_checked(user_id=user_id, at=now)
            if not automatic_reload_account_eligible(account):
                return None
            self._pause_failed_purchase(session, user_id)
            if (
                not preferences.reload_enabled
                or repository.reload_state(user_id).paused_purchase_id
            ):
                return None
            purchases = CreditPurchaseRepository(session)
            if purchases.pending_automatic(user_id=user_id) is not None:
                return None
            balance = BillingCreditRepository(session).balance(
                user_id=user_id,
                at=now,
            )
            if balance > preferences.reload_threshold_cents * (NANOS_PER_USD // 100):
                return None
            prepared = CreditPurchaseService(self.database, self.payments).prepare_in_session(
                session,
                user_id=user_id,
                request_key=str(uuid4()),
                amount_cents=preferences.reload_amount_cents,
                kind=CreditPurchaseKind.Automatic,
            )
            return str(prepared.id)

    @staticmethod
    def _account(session: Session, user_id: str) -> None:
        if BillingAccountRepository(session).get_by_user(user_id, for_update=True) is None:
            raise NotFoundError("the billing account does not exist")

    @staticmethod
    def _pause_failed_purchase(session: Session, user_id: str) -> None:
        repository = BillingPreferencesRepository(session)
        state = repository.reload_state(user_id)
        if state.paused_purchase_id is not None:
            return
        failure = CreditPurchaseRepository(session).latest_automatic_failure(
            user_id=user_id,
            since=state.resumed_at,
        )
        if failure is not None:
            repository.pause_reload(
                user_id=user_id,
                purchase_id=failure.id,
                reason=AutomaticReloadPauseReason(failure.status),
            )


__all__ = ["AutomaticReloadResult", "AutomaticReloadService"]
