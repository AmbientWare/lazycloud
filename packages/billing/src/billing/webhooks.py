from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_periods import BillingPeriodRepository
from database.repositories.billing_webhook_events import BillingWebhookEventRepository
from shared.payments import PaymentEvent, PaymentProvider, ProviderInvoice
from sqlalchemy.orm import Session

from billing.invoices import payment_outcome, to_cents
from billing.standing import reconcile_standing

LOGGER = logging.getLogger(__name__)

INVOICE_EVENT_PREFIX = "invoice."

CARD_SAVED_EVENTS = frozenset({"setup_intent.succeeded", "payment_method.attached"})
"""Deliveries that say a customer now has a card this platform can charge.

Both, because either can arrive first and neither is guaranteed: the hosted page
attaches the method and then succeeds the intent, and a customer replacing a card
through the portal produces only the attach. Acting on both is idempotent —
setting the same default twice is one state.
"""


@dataclass(frozen=True, slots=True)
class BillingWebhookService:
    """What a payment outcome from the provider means for an account.

    Every delivery is read as *something about this invoice changed*, never as a
    statement of what it changed to. The invoice is fetched back and the account
    is reconciled against what the provider says now.

    That is not caution about forgery — the signature settles that — it is that a
    delivery is a snapshot of a moment that has already passed. Deliveries arrive
    out of order and are retried for days, so acting on the body would let a
    stale failure land after the payment that fixed it and leave an account
    barred for money it has already paid.
    """

    session: Session
    payments: Callable[[], PaymentProvider]
    """Resolved only once a delivery turns out to concern an invoice this
    platform issued. Most do not — an endpoint carries every event it is
    subscribed to, and a payment credential that has not been configured must not
    turn a delivery this platform is right to ignore into a failed one."""

    def apply(self, *, event: PaymentEvent) -> bool:
        """Act on one delivery, reporting whether it changed anything.

        The claim is taken last and shares the caller's transaction, so a
        delivery either applies and is recorded or does neither. Claiming first
        would mean a delivery that could not be applied yet — an outcome arriving
        before the close committed the invoice it names — was recorded as handled
        and never retried.
        """

        if event.type in CARD_SAVED_EVENTS:
            return self._remember_card(event)
        if not event.type.startswith(INVOICE_EVENT_PREFIX):
            # Nothing else is subscribed to, and an endpoint is easy to point at
            # more than it needs.
            return False
        periods = BillingPeriodRepository(self.session)
        period = periods.get_by_provider_invoice(event.object_id)
        if period is None:
            # Either an invoice this platform did not issue — the same provider
            # account can carry other integrations — or one whose close has not
            # committed yet. Unclaimed either way, so a retry can still find it.
            LOGGER.info(
                "billing: %s names invoice %s, which no period claims",
                event.id,
                event.object_id,
            )
            return False
        if not BillingWebhookEventRepository(self.session).claim(
            event_id=event.id, event_type=event.type
        ):
            LOGGER.info("billing: %s already applied", event.id)
            return False

        invoice = self.payments().fetch_invoice(provider_invoice_id=event.object_id)
        self._reconcile_amount(invoice, expected_cents=to_cents(period.charged_cost_nanos))
        outcome = payment_outcome(invoice)
        if outcome is not None:
            periods.settle(user_id=period.user_id, period_start=period.period_start, status=outcome)
        reconcile_standing(self.session, user_id=period.user_id)
        return outcome is not None

    def _remember_card(self, event: PaymentEvent) -> bool:
        """Make the card a customer just saved the one their invoices are charged to.

        Saving a card does not make it the default, and nothing at the provider
        does it implicitly — a customer who completed the hosted page and had this
        step skipped is indistinguishable from one who never saved a card, until
        the charge fails for want of one.

        Done from the delivery rather than by polling because there is no moment
        this platform would otherwise know to look: the customer leaves for the
        hosted page and may never come back to the page that sent them.
        """

        if not event.customer_id or not event.payment_method_id:
            return False
        account = BillingAccountRepository(self.session).get_by_provider_customer(event.customer_id)
        if account is None:
            # A customer of some other integration on the same provider account.
            return False
        if not BillingWebhookEventRepository(self.session).claim(
            event_id=event.id, event_type=event.type
        ):
            return False
        payments = self.payments()
        owner = payments.payment_method_owner(provider_payment_method_id=event.payment_method_id)
        if owner != event.customer_id:
            # The card this delivery is about is no longer theirs. Deliveries are
            # retried for days, so one about a card the customer has since
            # replaced arrives after the replacement — and setting it would put
            # the old card back and charge next month to it.
            LOGGER.info("billing: %s names a card that is no longer on that customer", event.id)
            return False
        payments.set_default_payment_method(
            provider_customer_id=event.customer_id,
            provider_payment_method_id=event.payment_method_id,
        )
        LOGGER.info("billing: %s now has a card on file", account.user_id)
        return True

    def _reconcile_amount(self, invoice: ProviderInvoice, *, expected_cents: int) -> None:
        """Say loudly when the provider's figure is not the one this platform set.

        This platform decides the amount and the provider only collects it, so
        the two agreeing is not a coincidence to be verified politely — a
        disagreement means a customer was charged something nobody here computed.
        Reported rather than raised: the money has already moved, and refusing to
        record it would leave the platform's own view wrong as well.
        """

        if invoice.total_cents != expected_cents:
            LOGGER.error(
                "billing: invoice %s totals %d cents, but the period it settles owes %d",
                invoice.provider_invoice_id,
                invoice.total_cents,
                expected_cents,
            )


__all__ = ["BillingWebhookService"]
