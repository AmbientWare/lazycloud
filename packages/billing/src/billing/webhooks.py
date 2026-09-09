from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_webhook_events import BillingWebhookEventRepository
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.payments import PaymentEvent, ProviderSubscription, SubscriptionPaymentProvider
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from billing.periods import carry_plan_into_cycle

LOGGER = logging.getLogger(__name__)

CARD_SAVED_EVENTS = frozenset({"setup_intent.succeeded", "payment_method.attached"})
"""Deliveries that say a customer now has a card this platform can charge.

Both, because either can arrive first and neither is guaranteed: the hosted page
attaches the method and then succeeds the intent, and a customer replacing a card
through the portal produces only the attach. Acting on both is idempotent —
setting the same default twice is one state.
"""

SUBSCRIPTION_STANDING_EVENTS = frozenset(
    {
        "invoice.paid",
        "invoice.payment_failed",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)
"""Deliveries that say where a subscribed account now stands.

All four are read the same way — as a cue to go and look at the subscription —
which is why an invoice event needs no invoice: the subscription is what says
whether this account is paid up, what cycle it is in, and whether it still
exists. That also makes the four idempotent and order-independent between
themselves, since none of them is believed about anything.
"""

RUNNING_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})
"""Provider words for a subscription that is paid up and billing."""

UNPAID_SUBSCRIPTION_STATUSES = frozenset({"past_due", "unpaid", "incomplete"})
"""Provider words for a subscription whose payment did not go through.

Standing this platform refuses new work on, and nothing more: retrying the card
is the provider's, and an opinion here about when to try again would be a second
collection engine beside theirs.
"""

ENDED_SUBSCRIPTION_STATUSES = frozenset({"canceled", "incomplete_expired"})
"""Provider words for a subscription that is over.

Clearing the subscription and the plan off the row is the whole of what this
decides, and it clears both because they say one thing together: what this
account is on. A row that kept its plan would show a customer the terms of a
subscription that no longer exists, offer them no way back onto one, and satisfy
an admission check asking whether their usage has anywhere to land.

The standing stands, because a subscription ending says nothing about whether the
money already owed was collected. So does the grant, which is what funds the
final invoice the provider is about to raise. A row left naming no subscription
is a row the next billing surface provisions again.
"""


@dataclass(frozen=True, slots=True)
class BillingWebhookService:
    """What a delivery from the payment provider means for an account.

    Every delivery is read as *something about this object changed*, never as a
    statement of what it changed to. The object is fetched back and acted on
    against what the provider says now.

    That is not caution about forgery — the signature settles that — it is that a
    delivery is a snapshot of a moment that has already passed. Deliveries arrive
    out of order and are retried for days, so acting on the body would let a
    stale change land after the one that superseded it.
    """

    session: Session
    payments: Callable[[], SubscriptionPaymentProvider]
    """Resolved only once a delivery turns out to concern an account this
    platform holds. Most do not — an endpoint carries every event it is
    subscribed to, and a payment credential that has not been configured must not
    turn a delivery this platform is right to ignore into a failed one."""

    def apply(self, *, event: PaymentEvent) -> bool:
        """Act on one delivery, reporting whether it changed anything.

        The claim is taken last and shares the caller's transaction, so a
        delivery either applies and is recorded or does neither. Claiming first
        would mean a delivery that could not be applied yet was recorded as
        handled and never retried.
        """

        if event.type in CARD_SAVED_EVENTS:
            return self._remember_card(event)
        if event.type in SUBSCRIPTION_STANDING_EVENTS:
            return self._read_standing(event)
        # Nothing else is acted on, and an endpoint is easy to point at more than
        # it needs.
        return False

    def _remember_card(self, event: PaymentEvent) -> bool:
        """Make the card a customer just saved the one their charges go to.

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
        accounts = BillingAccountRepository(self.session)
        # Locked, unlike the read this used to take: what an account may spend
        # before it can be charged is decided from the card, so this now writes,
        # and a standing delivery settling the same row concurrently must not
        # interleave with it.
        account = accounts.get_by_provider_customer(event.customer_id, for_update=True)
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
        if not accounts.set_payment_method_present(
            user_id=account.user_id, present=True, at=utc_now()
        ):
            return True
        LOGGER.info("billing: %s now has a card on file", account.user_id)
        return True

    def _read_standing(self, event: PaymentEvent) -> bool:
        """Settle where a subscribed account stands, from the subscription itself.

        An invoice delivery is not read as an invoice. What a failed payment or a
        renewal changes is the subscription, and the subscription is a single
        object that answers all of it — paid up or not, which cycle, still there
        or gone. Reading that instead of the delivery is why four events collapse
        into one path and why none of them can arrive too late to be safe: a
        payment failure retried for days lands on whatever the account is now,
        not on what it was when the delivery was written.
        """

        accounts = BillingAccountRepository(self.session)
        # Locked, because what is written next is decided from what is read here
        # and a renewal and its invoice arrive together.
        account = accounts.get_by_provider_customer(event.customer_id, for_update=True)
        if account is None or not account.provider_subscription_id:
            # Somebody else's customer on the same provider account, or one of
            # ours that was never provisioned — an invoice for an account naming
            # no subscription says nothing about a subscription.
            return False
        if not BillingWebhookEventRepository(self.session).claim(
            event_id=event.id, event_type=event.type
        ):
            return False
        payments = self.payments()
        subscription = payments.subscription(
            provider_subscription_id=account.provider_subscription_id
        )
        if subscription.status in RUNNING_SUBSCRIPTION_STATUSES:
            return self._carry_the_period(payments, account, subscription)
        if subscription.status in UNPAID_SUBSCRIPTION_STATUSES:
            accounts.upsert(
                user_id=account.user_id,
                status=BillingAccountStatus.PastDue,
                provider_customer_id=account.provider_customer_id,
                provider_subscription_id=account.provider_subscription_id,
                provider_credit_grant_id=account.provider_credit_grant_id,
                plan=account.plan,
                subscription_terms_version=account.subscription_terms_version,
                scheduled_terms_version=account.scheduled_terms_version,
                scheduled_change_at=account.scheduled_change_at,
            )
            LOGGER.info("billing: %s is past due on its subscription", account.user_id)
            return True
        if subscription.status in ENDED_SUBSCRIPTION_STATUSES:
            accounts.upsert(
                user_id=account.user_id,
                # Carried through rather than settled: a subscription ending does
                # not say whether the money it already owes was paid.
                status=account.status,
                provider_customer_id=account.provider_customer_id,
                provider_subscription_id="",
                # Left naming the grant, which funds the final invoice the
                # provider raises for the part-cycle this ends.
                provider_credit_grant_id=account.provider_credit_grant_id,
                plan=None,
                subscription_terms_version=None,
                scheduled_terms_version=None,
                scheduled_change_at=None,
            )
            LOGGER.info("billing: %s is no longer subscribed", account.user_id)
            return True
        # A word this platform has not been taught. Declining to act keeps the
        # provider free to add to its own vocabulary without a delivery becoming
        # a rejection, and leaves the account exactly as it was.
        LOGGER.info(
            "billing: subscription for %s reads %s, which decides nothing here",
            account.user_id,
            subscription.status,
        )
        return False

    def _carry_the_period(
        self,
        payments: SubscriptionPaymentProvider,
        account: BillingAccount,
        subscription: ProviderSubscription,
    ) -> bool:
        """Give a running subscription the cycle and the allowance it is now in.

        The plan is the subscription's rather than the row's, so a plan changed
        at the provider — through their portal, or by a subscribe whose
        transaction died after the swap — reaches the row and the period it
        decides the terms of.

        Which of those a delivery is carrying decides what happens to the grant
        the account already holds, and `carry_plan_into_cycle` reads that off the
        period rather than off the delivery. A renewal opens a cycle and leaves
        the outgoing grant to fund the invoice finalizing at that moment; a plan
        change re-terms the cycle in progress, and leaving its grant alone would
        put two allowances on one cycle.
        """

        if subscription.plan is None:
            # A licensed price this platform did not publish. What the period is
            # worth and what the grant is sized to are both the plan's, so there
            # is nothing here to decide — and leaving the account as it was keeps
            # somebody else's subscription on this provider account from being
            # given terms of ours.
            LOGGER.info(
                "billing: subscription for %s carries no plan this platform published",
                account.user_id,
            )
            return False
        # A detached-card event has no customer, so refresh its presence from
        # the subscription owner when the cycle changes.
        has_card = payments.has_payment_method(provider_customer_id=account.provider_customer_id)
        accounts = BillingAccountRepository(self.session)
        if accounts.set_payment_method_present(
            user_id=account.user_id,
            present=has_card,
            at=utc_now(),
        ):
            LOGGER.info(
                "billing: %s now %s a card on file",
                account.user_id,
                "has" if has_card else "has no",
            )
        grant_id = carry_plan_into_cycle(
            self.session,
            payments,
            account_id=account.user_id,
            provider_customer_id=account.provider_customer_id,
            provider_credit_grant_id=account.provider_credit_grant_id,
            subscription=subscription,
            plan=subscription.plan,
        )
        if grant_id != account.provider_credit_grant_id:
            LOGGER.info("billing: %s starts a new subscription period", account.user_id)
        BillingAccountRepository(self.session).upsert(
            user_id=account.user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=account.provider_customer_id,
            provider_subscription_id=subscription.provider_subscription_id,
            provider_credit_grant_id=grant_id,
            plan=subscription.plan,
            subscription_terms_version=subscription.terms_version,
            scheduled_terms_version=subscription.scheduled_terms_version,
            scheduled_change_at=subscription.scheduled_change_at,
        )
        return True


__all__ = ["BillingWebhookService"]
