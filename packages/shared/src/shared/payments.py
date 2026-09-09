from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from pydantic import Field

from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_quotes import BilledDimension
from shared.contracts import ContractModel
from shared.credit_payments import CreditPayment, CreditPurchaseCheckout
from shared.enums import StringEnum

BILLING_CURRENCY = "USD"
"""The currency this platform bills in.

Named once because a hosted page has to be opened in one before anything is
charged, and a session opened in a currency nothing else uses is a customer who
cannot be billed through the card they just saved.
"""

METER_EVENT_NAMES: Mapping[BilledDimension, str] = {
    BilledDimension.ComputeRuntime: "lazycloud_compute_cost_nanos",
    BilledDimension.NetworkEgress: "lazycloud_egress_cost_nanos",
    BilledDimension.VolumeStorage: "lazycloud_volume_storage_cost_nanos",
}
"""What each dimension's usage is called at the provider.

Constants rather than settings: the command that publishes the catalog, the
process that queues meter events, and the one that reconciles an invoice have to
agree on these names, and an environment variable is how three processes come to
disagree. One meter per dimension so the invoice breaks down, which is also what
makes a line priced at zero visible as metered-and-free rather than absent.
"""


class SubscriptionChangeTiming(StringEnum):
    Immediate = "immediate"
    AtRenewal = "at_renewal"


class PaymentCustomer(ContractModel):
    """The provider's record of who pays.

    Its identifier is the only part this platform stores. Everything else about
    the customer — card, address, tax status — stays with the provider, which is
    what keeps this repository out of scope for cardholder data.
    """

    provider_customer_id: str = Field(min_length=1, max_length=255)


class PaymentEvent(ContractModel):
    """What one notification from the payment provider says changed.

    Deliberately a handful of fields rather than a model of the provider's event.
    Their payload is large, versioned on their schedule, and can grow a field at
    any time; a contract over it would be a second place their API is described
    and would reject a delivery for carrying something new.

    What arrives is treated as a claim that something changed, and the object is
    read back from the provider — so all this carries is enough to say what
    changed and to look it up.
    """

    id: str = Field(min_length=1, max_length=255)
    type: str = Field(min_length=1, max_length=128)
    object_id: str = Field(default="", max_length=255)
    customer_id: str = Field(default="", max_length=255)
    """Who the change is about, empty where the event names nobody."""

    payment_method_id: str = Field(default="", max_length=255)
    """The instrument a delivery says was saved, empty on everything else."""

    payment_id: str = Field(default="", max_length=255)
    credit_purchase_id: str = Field(default="", max_length=255)


class ProviderSubscription(ContractModel):
    """What a customer is subscribed to, and the period it is currently in.

    The period is the provider's rather than this platform's. The plan renews on
    their clock and the invoice covers what they say it covers, so an allowance
    or a reconciliation window derived from anything else would sit beside the
    invoice it belongs to instead of on top of it.
    """

    provider_subscription_id: str = Field(min_length=1, max_length=255)
    status: str = Field(min_length=1, max_length=64)
    """The provider's own word for where the subscription stands.

    Carried through rather than mapped to an enum, for the reason the event type
    is: the provider owns this vocabulary and adds to it, and a value no adapter
    had been taught would turn a delivery into a rejection. The domain owns the
    set it acts on, and a value outside it is a decision rather than a parse
    failure.
    """

    current_period_started_at: datetime
    current_period_ended_at: datetime
    plan: BillingPlanId | None = None
    terms_version: SubscriptionTermsVersion | None
    scheduled_terms_version: SubscriptionTermsVersion | None
    scheduled_change_at: datetime | None
    """Which published plan the subscription's licensed price names.

    `None` where it names a price this platform did not publish, which is a
    subscription nothing here can decide anything about — the allowance it would
    carry and the grant it would be given are both the plan's.
    """


class ProviderPaidSubscriptionPeriod(ContractModel):
    provider_invoice_id: str = Field(min_length=1)
    provider_invoice_line_id: str = Field(min_length=1)
    provider_subscription_id: str = Field(min_length=1)
    plan: BillingPlanId
    terms_version: SubscriptionTermsVersion
    period_started_at: datetime
    period_ended_at: datetime
    prorated: bool
    amount_nanos: int
    invoice_paid_nanos: int = Field(ge=0)
    paid_at: datetime


class ProviderInvoice(ContractModel):
    """One bill the provider raised, and the window it covers.

    The period is what a local figure is compared over, so it comes from the
    invoice rather than from a cycle computed here: an invoice covers what the
    provider says it covers, and a window derived from anything else would
    compare two different stretches of time and call the difference a
    disagreement.
    """

    provider_invoice_id: str = Field(min_length=1, max_length=255)
    status: str = Field(min_length=1, max_length=64)
    """The provider's own word for where the invoice stands.

    Carried through rather than mapped, for the reason a subscription's is: the
    provider owns this vocabulary and adds to it, and the domain owns the set it
    acts on.
    """

    period_started_at: datetime
    period_ended_at: datetime


class HostedPaymentSession(ContractModel):
    """A page at the provider for the customer to do something on.

    Only a URL comes back, deliberately. Saving a card and changing one both
    happen entirely at the provider, and the whole value of that is that no card
    detail ever reaches this platform — a session that returned anything about
    the instrument would be the beginning of losing that.
    """

    url: str = Field(min_length=1, max_length=2048)


class CreditPurchasePaymentProvider(Protocol):
    """Provider payment evidence for purchased prepaid credit."""

    def create_credit_purchase_checkout(
        self,
        *,
        provider_customer_id: str,
        purchase_id: str,
        amount_nanos: int,
        success_url: str,
        cancel_url: str,
    ) -> CreditPurchaseCheckout: ...

    def credit_purchase_checkout(self, *, provider_session_id: str) -> CreditPurchaseCheckout: ...

    def create_credit_purchase_payment(
        self, *, provider_customer_id: str, purchase_id: str, amount_nanos: int
    ) -> CreditPayment:
        """Create an unconfirmed payment; persist its identity before confirmation."""
        ...

    def confirm_credit_purchase_payment(self, *, provider_payment_id: str) -> CreditPayment: ...

    def cancel_credit_purchase_payment(self, *, provider_payment_id: str) -> CreditPayment: ...

    def credit_purchase_payment(self, *, provider_payment_id: str) -> CreditPayment: ...


class SubscriptionPaymentProvider(Protocol):
    """Customer relationships, subscriptions and their invoiced usage.

    Callers name published plans; the adapter resolves its catalog identifiers.
    Paid invoice lines are funding evidence, while a saved card is not.
    """

    def create_customer(self, *, account_id: str, email: str, workspace_id: str) -> PaymentCustomer:
        """Register a payer, returning the identifier to store against them.

        `account_id` names who is being registered, so the provider settles a
        repeat itself. A registration whose answer never arrived — a timeout, or
        a process that died before it wrote the identifier down — is retried, and
        with nothing naming the account that retry is indistinguishable from a
        second person: the provider registers a second customer, this platform
        stores one of them, and the invoices of one account are split across two
        records only one of which anything here can name.
        """
        ...

    def card_setup_session(
        self, *, provider_customer_id: str, currency: str, success_url: str, cancel_url: str
    ) -> HostedPaymentSession:
        """A hosted page where the customer saves a card for later charges.

        The card is collected and stored by the provider; what comes back here is
        the identifier of a payment method, never the card. That is what keeps
        this repository outside the scope of cardholder data rules, and it is why
        this returns a URL to redirect to rather than a form to render.
        """
        ...

    def customer_portal_session(
        self, *, provider_customer_id: str, return_url: str
    ) -> HostedPaymentSession:
        """A hosted page where the customer manages what this platform bills them.

        Replacing an expired card, and reading what they were charged. Hosted for
        the same reason as the setup page, and separate from it because the two
        answer different questions: one is for someone who has no card on file,
        the other for someone who does.
        """
        ...

    def payment_method_owner(self, *, provider_payment_method_id: str) -> str:
        """Which customer a saved instrument currently belongs to.

        Read back rather than taken from the notification that named it. A
        notification describes a moment that has already passed and is retried
        for days, so one about a card the customer has since replaced can arrive
        after the replacement — and acting on it would put the old card back.

        Empty where it belongs to nobody, which is what a detached instrument
        looks like.
        """
        ...

    def has_payment_method(self, *, provider_customer_id: str) -> bool:
        """Whether this customer has any instrument that could be charged.

        Asked of the provider rather than kept in step from notifications,
        because removing a card is the one direction those cannot report: the
        instrument is already detached by the time the notification describes it,
        so it names no customer and there is nothing to resolve it back to.

        Asked at a cycle boundary and nowhere near a container start. What it
        decides is how much an account is given for the cycle about to open, so
        once per cycle is exactly as often as the answer is used.
        """
        ...

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        """Make a saved card the one charges are taken from.

        A separate step because saving a card does not make it the default, and
        nothing does it implicitly: a customer who has completed the hosted page
        and had this step skipped looks identical to one who never saved a card,
        right up until the charge fails for want of one.
        """
        ...

    def record_meter_event(
        self,
        *,
        event_name: str,
        provider_customer_id: str,
        value_nanos: int,
        occurred_at: datetime,
        identifier: str,
        pricing_version: str,
    ) -> None:
        """Report one priced window of usage against a customer's meter.

        `identifier` is the usage record's own id, so a resend of an event the
        provider already accepted is discarded by them rather than counted twice
        — which is what makes at-least-once delivery the right guarantee here
        rather than a compromise. How long that protection lasts is the
        provider's own fact and bounds the retry schedule that sends these.

        `value_nanos` is the ledger segment's cost verbatim. Nothing is rederived
        on the way out, so an invoice and the ledger behind it compare as exact
        integers with no second rounding step to argue about.
        """
        ...

    def create_subscription(
        self, *, provider_customer_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        """Put a customer on a named plan and the metered prices that go with it.

        Which prices those are belongs to the adapter's published catalog. The
        caller names the plan and nothing else; naming a price would be a caller
        holding provider identifiers, and two callers naming them differently
        would be two customers on different subscriptions for one plan.

        Idempotent, and a customer who already holds a live subscription is
        answered with it rather than given a second — whatever plan that one
        carries, which the caller records instead of the plan it asked for. A
        second subscription carries the same metered prices, so one customer's
        usage would be counted onto two invoices. Converging on what the provider
        holds rather than on a repeat key, because a key is forgotten while an
        account is not, and a customer whose subscription has ended must be given
        a new one rather than the one that ended.
        """
        ...

    def set_subscription_plan(
        self,
        *,
        provider_subscription_id: str,
        terms_version: SubscriptionTermsVersion,
        timing: SubscriptionChangeTiming,
        operation_id: str,
        operation_created_at: datetime,
    ) -> ProviderSubscription:
        """Change exact subscription terms, preserving its cycle and metered items.

        Immediate changes require payment for the proration. Renewal changes
        retain current paid terms until their recorded boundary. Selecting the
        held version cancels a scheduled change. The durable operation identity
        fences retries across provider calls and local commits.
        """
        ...

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        """What the provider says the subscription is now.

        Read back rather than taken from the delivery that named it: deliveries
        are retried for days and arrive out of order, so one describing a period
        that has since rolled would move an allowance backwards.
        """
        ...

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        """What the provider billed as usage, keyed by meter event name.

        The only guard that what this platform sent is what it was charged for.
        Both sides are integer nanodollars, so the comparison is exact and a
        difference is a fact rather than a rounding argument. Lines with no meter
        behind them — the flat plan price — are not usage and are left out.
        """
        ...

    def invoices_for(
        self, *, provider_customer_id: str, since: datetime, limit: int | None = 12
    ) -> Sequence[ProviderInvoice]:
        """Customer invoices since an instant; None exhausts every page in the window."""
        ...

    def paid_subscription_periods(
        self,
        *,
        provider_customer_id: str,
        provider_subscription_id: str,
        since: datetime,
    ) -> Sequence[ProviderPaidSubscriptionPeriod]:
        """Paid invoice plan lines linked to the customer's current subscription."""
        ...


class PaymentProvider(SubscriptionPaymentProvider, CreditPurchasePaymentProvider, Protocol):
    """The payment adapter composed by application processes."""


__all__ = [
    "BILLING_CURRENCY",
    "METER_EVENT_NAMES",
    "CreditPurchasePaymentProvider",
    "HostedPaymentSession",
    "PaymentCustomer",
    "PaymentEvent",
    "PaymentProvider",
    "ProviderInvoice",
    "ProviderPaidSubscriptionPeriod",
    "ProviderSubscription",
    "SubscriptionChangeTiming",
    "SubscriptionPaymentProvider",
]
