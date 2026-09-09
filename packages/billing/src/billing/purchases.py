from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from database.client import DatabaseClient
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.credit_purchases import CreditPurchaseRepository
from database.tables.credit_purchases import CreditPurchaseTable
from shared.billing_credits import CreditGrant, CreditKind
from shared.billing_quotes import NANOS_PER_USD
from shared.credit_payments import (
    MAX_CREDIT_PURCHASE_CENTS,
    MIN_CREDIT_PURCHASE_CENTS,
    CreditPayment,
    CreditPaymentStatus,
    CreditPurchaseKind,
)
from shared.errors import (
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PaymentRequiredError,
)
from shared.http.billing import CreditPurchaseResponse
from shared.payments import CreditPurchasePaymentProvider, PaymentEvent
from shared.timestamps import to_utc, utc_now
from sqlalchemy.orm import Session

from billing.automatic_reload_policy import AutomaticPurchaseDecision, authorize_automatic_purchase

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CreditPurchaseService:
    database: DatabaseClient
    payments: Callable[[], CreditPurchasePaymentProvider]

    def create(
        self,
        *,
        user_id: str,
        request_key: str,
        amount_cents: int,
        kind: CreditPurchaseKind,
        success_url: str = "",
        cancel_url: str = "",
    ) -> CreditPurchaseResponse:
        with self.database.session() as session:
            purchase = self.prepare_in_session(
                session,
                user_id=user_id,
                request_key=request_key,
                amount_cents=amount_cents,
                kind=kind,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        return self.reconcile(user_id=user_id, purchase_id=str(purchase.id))

    def prepare_in_session(
        self,
        session: Session,
        *,
        user_id: str,
        request_key: str,
        amount_cents: int,
        kind: CreditPurchaseKind,
        success_url: str = "",
        cancel_url: str = "",
    ) -> CreditPurchaseResponse:
        if not MIN_CREDIT_PURCHASE_CENTS <= amount_cents <= MAX_CREDIT_PURCHASE_CENTS:
            raise InvalidInputError("credit purchase amount is outside the published limits")
        request_key = str(UUID(request_key))
        amount = amount_cents * (NANOS_PER_USD // 100)
        account = BillingAccountRepository(session).get_by_user(user_id, for_update=True)
        if account is None or not account.provider_customer_id:
            raise ConflictError("billing registration must finish before buying credit")
        purchases = CreditPurchaseRepository(session)
        row = purchases.by_request(user_id=user_id, request_key=request_key)
        if row is not None:
            if (row.amount_nanos, row.kind, row.success_url, row.cancel_url) != (
                amount,
                kind.value,
                success_url,
                cancel_url,
            ):
                raise ConflictError("a purchase request cannot be reused with different terms")
        else:
            row = CreditPurchaseTable(
                id=str(uuid4()),
                user_id=user_id,
                request_key=request_key,
                kind=kind.value,
                amount_nanos=amount,
                status=CreditPaymentStatus.Pending.value,
                provider_customer_id=account.provider_customer_id,
                success_url=success_url,
                cancel_url=cancel_url,
                creation_started_at=utc_now(),
            )
            purchases.add(row)
        return _response(row)

    def get(self, *, user_id: str, purchase_id: str) -> CreditPurchaseResponse:
        with self.database.session() as session:
            row = CreditPurchaseRepository(session).get(purchase_id=purchase_id, user_id=user_id)
            if row is None:
                raise NotFoundError("credit purchase not found")
            return _response(row)

    def cancel_automatic(self, *, user_id: str, purchase_id: str) -> CreditPurchaseResponse:
        with self.database.session() as session:
            row = self._locked(session, user_id=user_id, purchase_id=purchase_id)
            if row.kind != CreditPurchaseKind.Automatic.value:
                raise ConflictError("only an automatic reload can be cancelled here")
            if row.provider_payment_id is None:
                row.status = CreditPaymentStatus.Cancelled.value
            else:
                payment = self.payments().cancel_credit_purchase_payment(
                    provider_payment_id=row.provider_payment_id
                )
                self._settle(session, row, payment)
            row.updated_at = utc_now()
            return _response(row)

    def reconcile(self, *, user_id: str, purchase_id: str) -> CreditPurchaseResponse:
        payments = self.payments()
        # Persist the provider identifier before confirmation can collect money.
        with self.database.session() as session:
            row = self._locked(session, user_id=user_id, purchase_id=purchase_id)
            if row.provider_payment_id is None:
                if row.provider_session_id is not None:
                    checkout = payments.credit_purchase_checkout(
                        provider_session_id=row.provider_session_id
                    )
                else:
                    if row.status in (
                        CreditPaymentStatus.Declined.value,
                        CreditPaymentStatus.Cancelled.value,
                    ):
                        return _response(row)
                    started = row.creation_started_at
                    if started is None or utc_now() - to_utc(started) >= timedelta(hours=20):
                        raise ConflictError(
                            "payment creation needs reconciliation before it can be retried"
                        )
                    if row.kind == CreditPurchaseKind.Automatic.value:
                        decision = authorize_automatic_purchase(
                            session, purchase=row, now=utc_now()
                        )
                        if decision is not AutomaticPurchaseDecision.Allowed:
                            row.status = CreditPaymentStatus.Cancelled.value
                            row.updated_at = utc_now()
                            return _response(row)
                        try:
                            payment = payments.create_credit_purchase_payment(
                                provider_customer_id=row.provider_customer_id,
                                purchase_id=row.id,
                                amount_nanos=row.amount_nanos,
                            )
                        except PaymentRequiredError:
                            row.status = CreditPaymentStatus.Declined.value
                            row.updated_at = utc_now()
                            return _response(row)
                        self._validate_payment(row, payment)
                        row.provider_payment_id = payment.provider_payment_id
                        checkout = None
                    else:
                        checkout = payments.create_credit_purchase_checkout(
                            provider_customer_id=row.provider_customer_id,
                            purchase_id=row.id,
                            amount_nanos=row.amount_nanos,
                            success_url=row.success_url,
                            cancel_url=row.cancel_url,
                        )
                if checkout is not None:
                    if (
                        checkout.purchase_id != row.id
                        or checkout.provider_customer_id != row.provider_customer_id
                        or row.provider_session_id not in (None, checkout.provider_session_id)
                    ):
                        raise ConflictError("checkout evidence does not match the credit purchase")
                    row.provider_session_id = checkout.provider_session_id
                    row.hosted_url = checkout.url
                    row.session_expires_at = checkout.expires_at
                    row.provider_payment_id = checkout.provider_payment_id or None
                    if checkout.expired and row.provider_payment_id is None:
                        row.status = CreditPaymentStatus.Cancelled.value
            row.updated_at = utc_now()
            if row.provider_payment_id is None:
                return _response(row)
        with self.database.session() as session:
            row = self._locked(session, user_id=user_id, purchase_id=purchase_id)
            if not row.provider_payment_id:
                raise ConflictError("credit purchase has no recorded payment")
            payment = payments.credit_purchase_payment(provider_payment_id=row.provider_payment_id)
            if (
                row.kind == CreditPurchaseKind.Automatic.value
                and row.status == CreditPaymentStatus.Pending.value
                and payment.status is CreditPaymentStatus.Pending
                and payment.confirmation_required
            ):
                decision = authorize_automatic_purchase(session, purchase=row, now=utc_now())
                if decision is AutomaticPurchaseDecision.Allowed:
                    payment = payments.confirm_credit_purchase_payment(
                        provider_payment_id=row.provider_payment_id
                    )
                else:
                    payment = payments.cancel_credit_purchase_payment(
                        provider_payment_id=row.provider_payment_id
                    )
            self._settle(session, row, payment)
            row.updated_at = utc_now()
            row.last_error = ""
            return _response(row)

    def apply_event(self, event: PaymentEvent) -> bool:
        if not event.payment_id and not event.credit_purchase_id:
            return False
        with self.database.session() as session:
            purchases = CreditPurchaseRepository(session)
            row = purchases.by_payment(payment_id=event.payment_id)
            if row is None and event.credit_purchase_id:
                try:
                    purchase_id = str(UUID(event.credit_purchase_id))
                except ValueError:
                    return False
                row = purchases.by_id(purchase_id)
            if row is None:
                return False
            user_id, purchase_id = row.user_id, row.id
        if event.payment_id:
            with self.database.session() as session:
                row = self._locked(session, user_id=user_id, purchase_id=purchase_id)
                payment = self.payments().credit_purchase_payment(
                    provider_payment_id=event.payment_id
                )
                self._validate_payment(row, payment)
                row.provider_payment_id = payment.provider_payment_id
                self._settle(session, row, payment)
                row.updated_at = utc_now()
                row.last_error = ""
            return True
        self.reconcile(user_id=user_id, purchase_id=purchase_id)
        return True

    def sweep(self, *, limit: int = 100) -> int:
        with self.database.session() as session:
            due = CreditPurchaseRepository(session).due(now=utc_now(), limit=limit)
        settled = 0
        for purchase_id, user_id in due:
            try:
                self.reconcile(user_id=user_id, purchase_id=purchase_id)
                settled += 1
            except DomainError as exc:
                with self.database.session() as session:
                    row = self._locked(session, user_id=user_id, purchase_id=purchase_id)
                    row.last_error = exc.code[:255]
                    row.updated_at = utc_now()
                LOGGER.warning(
                    "credit purchase reconciliation failed: %s %s", purchase_id, exc.code
                )
        return settled

    def _locked(self, session: Session, *, user_id: str, purchase_id: str) -> CreditPurchaseTable:
        account = BillingAccountRepository(session).get_by_user(user_id, for_update=True)
        row = CreditPurchaseRepository(session).get(purchase_id=purchase_id, user_id=user_id)
        if account is None or row is None:
            raise NotFoundError("credit purchase not found")
        if account.provider_customer_id != row.provider_customer_id:
            raise ConflictError("credit purchase belongs to a different payment relationship")
        return row

    @staticmethod
    def _validate_payment(row: CreditPurchaseTable, payment: CreditPayment) -> None:
        if (
            payment.purchase_id != row.id
            or payment.provider_customer_id != row.provider_customer_id
            or payment.amount_nanos != row.amount_nanos
            or row.provider_payment_id not in (None, payment.provider_payment_id)
        ):
            raise ConflictError("payment evidence does not match the credit purchase")

    def _settle(self, session: Session, row: CreditPurchaseTable, payment: CreditPayment) -> None:
        self._validate_payment(row, payment)
        row.status = payment.status.value
        if payment.status is not CreditPaymentStatus.Succeeded:
            if row.credit_lot_id is not None:
                raise ConflictError("a funded purchase no longer has captured payment evidence")
            return
        credits = BillingCreditRepository(session)
        if row.credit_lot_id is None:
            funded_at = utc_now()
            row.funded_at = funded_at
            row.credit_lot_id = credits.issue(
                user_id=row.user_id,
                grant=CreditGrant(
                    source_id=f"payment:{payment.provider_payment_id}",
                    kind=CreditKind.Purchased,
                    amount_nanos=row.amount_nanos,
                    effective_at=funded_at,
                ),
            )
        reversal = row.amount_nanos - payment.retained_nanos
        delta = row.reversed_nanos - reversal
        if delta:
            row.reversal_sequence += 1
            credits.adjust(
                user_id=row.user_id,
                credit_lot_id=row.credit_lot_id,
                source_id=f"payment-adjustment:{row.id}:{row.reversal_sequence}",
                amount_nanos=delta,
                effective_at=utc_now(),
            )
            row.reversed_nanos = reversal


def _response(row: CreditPurchaseTable) -> CreditPurchaseResponse:
    return CreditPurchaseResponse(
        id=UUID(row.id),
        amount_nanos=row.amount_nanos,
        status=CreditPaymentStatus(row.status),
        checkout_url=(row.hosted_url if row.status == CreditPaymentStatus.Pending.value else None),
        funded_at=row.funded_at,
        reversed_nanos=row.reversed_nanos,
    )
