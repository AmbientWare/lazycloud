from __future__ import annotations

from typing import Protocol

from pydantic import Field

from shared.contracts import ContractModel


class PaymentCustomer(ContractModel):
    """The provider's record of who pays.

    Its identifier is the only part this platform stores. Everything else about
    the customer — card, address, tax status — stays with the provider, which is
    what keeps this repository out of scope for cardholder data.
    """

    provider_customer_id: str = Field(min_length=1, max_length=255)


class InvoiceLine(ContractModel):
    """One line the customer reads on their invoice.

    `amount_cents` is signed: an allowance is a negative line rather than a
    quantity netted out of a positive one, so the invoice says what was used and
    what was covered instead of only their difference.
    """

    description: str = Field(min_length=1, max_length=255)
    amount_cents: int


class ProviderInvoice(ContractModel):
    provider_invoice_id: str = Field(min_length=1, max_length=255)
    total_cents: int
    status: str = Field(min_length=1, max_length=32)
    paid: bool = False
    """Whether the money arrived, in full.

    A boolean rather than a status string so the judgement is made once, in the
    adapter that knows the provider's vocabulary, instead of by every caller
    comparing values it would have to keep in step. Part-payment is not paid:
    the month is still owed the remainder."""

    attempted: bool = False
    """Whether collection has been tried at all.

    What separates an invoice waiting to be paid from one that failed. Both read
    as unpaid, and treating them alike either marks an account behind the moment
    it is billed or never marks it behind at all."""


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


class HostedPaymentSession(ContractModel):
    """A page at the provider for the customer to do something on.

    Only a URL comes back, deliberately. Saving a card and changing one both
    happen entirely at the provider, and the whole value of that is that no card
    detail ever reaches this platform — a session that returned anything about
    the instrument would be the beginning of losing that.
    """

    url: str = Field(min_length=1, max_length=2048)


class PaymentProvider(Protocol):
    """Where money moves, and nothing else.

    Provider-neutral by construction, and deliberately narrow: this platform
    decides what is owed and hands over finished amounts. Nothing here returns a
    price or holds a rate, so swapping providers cannot change a bill.
    """

    def create_customer(self, *, email: str, workspace_id: str) -> PaymentCustomer:
        """Register a payer, returning the identifier to store against them."""
        ...

    def draft_invoice(self, *, provider_customer_id: str, period_key: str) -> ProviderInvoice:
        """Open a draft for one period, or return the draft already open for it.

        Keyed on the period so a retried close finds its own draft rather than
        opening a second one beside it.
        """
        ...

    def replace_invoice_lines(
        self,
        *,
        provider_invoice_id: str,
        provider_customer_id: str,
        currency: str,
        lines: tuple[InvoiceLine, ...],
    ) -> None:
        """Make the draft say exactly these lines and nothing else.

        Replaces rather than appends, because a period is recomputed and the
        second run must state what is owed rather than add to it.
        """
        ...

    def finalize_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        """Issue the invoice. After this the amount is what the customer owes."""
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

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        """Make a saved card the one invoices are charged to.

        A separate step because saving a card does not make it the default, and
        nothing does it implicitly: a customer who has completed the hosted page
        and had this step skipped looks identical to one who never saved a card,
        right up until the charge fails for want of one.
        """
        ...

    def pay_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        """Collect what an issued invoice states, from the payer's stored method.

        Attempted here rather than left to the provider's own schedule, because
        the result comes back in the response: the period records what happened
        while the platform is still looking at it, instead of learning it later
        from a notification that may not arrive for hours.

        Returns the invoice as it stands after the attempt. A refusal by the
        payer's bank is an outcome, not a transport failure — the invoice comes
        back unpaid and attempted, and the caller records that.
        """
        ...

    def fetch_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        """Read back what the provider currently says about an invoice.

        The authority for a payment outcome. A notification that something
        changed can arrive twice, out of order, or from anyone who can reach the
        endpoint, so what it says is treated as a claim and this is what settles
        it — the state at the moment of asking, not the state some earlier
        message described.
        """
        ...


__all__ = [
    "HostedPaymentSession",
    "InvoiceLine",
    "PaymentCustomer",
    "PaymentEvent",
    "PaymentProvider",
    "ProviderInvoice",
]
