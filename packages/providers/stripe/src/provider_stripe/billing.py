from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import Field
from shared.billing_plans import BillingPlanId
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.payments import (
    BILLING_CURRENCY,
    HostedPaymentSession,
    PaymentCustomer,
    ProviderCreditGrant,
    ProviderSubscription,
)
from shared.timestamps import utc_now

from provider_stripe.api import FormFields, StripeObject, read, send
from provider_stripe.catalog import (
    NANOS_PER_CENT,
    cents,
    plan_for_price_lookup_key,
    plan_line,
    subscription_price_lookup_keys,
)

CREDIT_GRANT_NAME = "Included usage"
"""What the customer sees the allowance called on their invoice."""

CREDIT_GRANT_SETTLEMENT_GRACE = timedelta(days=3)
"""How far a grant's applicable window is shifted past the period it funds.

Stripe applies credit when an invoice is *finalized*, not when it is raised, and
a subscription invoice finalizes about an hour after the period ends. A grant
expiring exactly at the boundary is therefore already gone when the invoice it
was bought for asks for it, and the next period's grant pays the last period's
arrears — so a customer's included usage silently shrinks by whatever they
overran the month before. Observed against a real account: the credit
transaction for cycle one named the grant issued for cycle two.

The shift applies to both ends, and the second end is what keeps the first from
recreating the problem it fixes. Extending only the expiry leaves two grants live
at the moment a period's invoice finalizes — the one bought for it and the one
bought for the period that has just begun — and an invoice that overran its
allowance takes the difference out of the next period's grant. Overrun is the
design here rather than an edge case, so that is the ordinary path and not a rare
one. Becoming effective a grace after its own period starts means a grant is not
yet spendable when the previous period's invoice finalizes, and is spendable long
before its own does.

Days rather than hours because the cost of being late is a customer billed
against the wrong month's allowance, and an hour is what Stripe takes to finalize
on a good day rather than a bound anyone stated.
"""

METER_EVENT_BACKFILL_DAYS = 35
"""How far back Stripe accepts a meter event's timestamp.

A queued event older than this can never be accepted however many times it is
retried, which is what makes an age check a reason to stop rather than a reason
to wait.

Observed against a real account rather than read from a page: an event stamped
34 days back was accepted and one stamped 36 days back was refused with "The
event timestamp cannot be more than 35 days in past". The operator command that
prices a metered backlog refuses a window starting beyond it, because usage
priced into the ledger that no meter event can carry is a charge nobody can
collect.
"""

METER_EVENT_DEDUPLICATION_HOURS = 24
"""How long Stripe enforces uniqueness on a meter event's `identifier`.

At-least-once delivery is safe only inside this window: a resend after it has
passed is counted a second time. It is the ceiling every retry schedule that
sends meter events has to fit under.
"""

_CUSTOMER_REGISTRATION_KEY_PREFIX = "customer-registration-"
_CREDIT_GRANT_KEY_PREFIX = "credit-grant-"
"""What each write's idempotency key is namespaced by.

A key is unique across everything one credential does at Stripe rather than per
endpoint, so an account id sent bare would collide with the same id used to key
a different operation for the same account.

Stripe keeps a key for a day, which is what these protect against: a request
whose answer never arrived, retried inside that window. They are not the durable
protection — the account row and the lock held over it are — and past the window
a retry writes again.

A key is only ever derived from something that changes whenever the write is
meant to happen again. Subscribing has no such value, so it is made idempotent by
reading first instead: keyed on the account alone, a customer whose subscription
had ended would be answered for a whole day with the subscription that ended
rather than the new one they asked for.
"""


class _Customer(StripeObject):
    id: str = Field(min_length=1)


class _HostedSession(StripeObject):
    url: str = ""


class _PaymentMethod(StripeObject):
    customer: str | None = None


class _Recurring(StripeObject):
    meter: str | None = None


class _Price(StripeObject):
    """The parts of a price this package reads, wherever one is carried.

    One shape for all three places Stripe returns a price — a subscription item,
    a listing, an invoice line — because each carries the same object and naming
    it three times is three descriptions of one thing to keep in step.
    """

    id: str
    lookup_key: str | None = None
    recurring: _Recurring | None = None


class _SubscriptionItem(StripeObject):
    id: str
    price: _Price
    current_period_start: int
    current_period_end: int


class _SubscriptionItems(StripeObject):
    data: list[_SubscriptionItem] = Field(default_factory=list)


class _Subscription(StripeObject):
    id: str
    status: str
    items: _SubscriptionItems


class _SubscriptionList(StripeObject):
    data: list[_Subscription] = Field(default_factory=list)


class _PriceList(StripeObject):
    data: list[_Price] = Field(default_factory=list)


class _Monetary(StripeObject):
    value: int


class _GrantAmount(StripeObject):
    monetary: _Monetary | None = None


class _CreditGrant(StripeObject):
    id: str
    amount: _GrantAmount
    expires_at: int | None = None
    voided_at: int | None = None


class _PriceDetails(StripeObject):
    price: _Price


class _LinePricing(StripeObject):
    price_details: _PriceDetails


class _InvoiceLine(StripeObject):
    id: str
    quantity_decimal: str | None = None
    pricing: _LinePricing | None = None


class _InvoiceLines(StripeObject):
    data: list[_InvoiceLine] = Field(default_factory=list)
    has_more: bool = False


class _Meter(StripeObject):
    id: str
    event_name: str


@dataclass(frozen=True, slots=True)
class StripeBilling:
    """The payment relationship behind one billing account.

    Card details never reach this process: the hosted pages collect and hold
    them, and what comes back is an identifier.
    """

    client: httpx.Client

    def create_customer(self, *, account_id: str, email: str, workspace_id: str) -> PaymentCustomer:
        """Register a payer, for as long as Stripe remembers the key, only once.

        Stripe keeps an idempotency key for 24 hours, which is why the identifier
        is stored in the request that registered it rather than reconciled by a
        later sweep: past that window a retry registers again and there is
        nothing to recognise it by.
        """

        data = [
            # Stripe is the system of record for who pays; this repository is the
            # system of record for what they used. The workspace is stamped here
            # so a payment reaching us out of band—a webhook, a dispute—can be
            # traced back without a second lookup table.
            ("metadata[workspace_id]", workspace_id),
        ]
        # Omitted rather than sent empty: an account whose identity provider
        # exposes no verified address is a real one, and a customer with no email
        # is the truthful record of that. An empty value is a value Stripe may
        # reject, and a rejection here is a person who cannot register at all.
        if email:
            data.append(("email", email))
        customer = read(
            _Customer,
            self.client,
            "POST",
            "/customers",
            data=data,
            idempotency_key=f"{_CUSTOMER_REGISTRATION_KEY_PREFIX}{account_id}",
        )
        return PaymentCustomer(provider_customer_id=customer.id)

    def card_setup_session(
        self, *, provider_customer_id: str, currency: str, success_url: str, cancel_url: str
    ) -> HostedPaymentSession:
        session = read(
            _HostedSession,
            self.client,
            "POST",
            "/checkout/sessions",
            data=[
                ("mode", "setup"),
                ("customer", provider_customer_id),
                # Required even in setup mode, where nothing is charged: Stripe
                # refuses the session without it.
                ("currency", currency.lower()),
                # Pinned. Left open, Stripe offers whatever wallets the account
                # has enabled, and several of them cannot be charged off-session
                # later — which is the entire purpose of saving one here.
                ("payment_method_types[0]", "card"),
                ("success_url", success_url),
                ("cancel_url", cancel_url),
            ],
        )
        return HostedPaymentSession(url=session.url)

    def customer_portal_session(
        self, *, provider_customer_id: str, return_url: str
    ) -> HostedPaymentSession:
        session = read(
            _HostedSession,
            self.client,
            "POST",
            "/billing_portal/sessions",
            data=[("customer", provider_customer_id), ("return_url", return_url)],
        )
        return HostedPaymentSession(url=session.url)

    def payment_method_owner(self, *, provider_payment_method_id: str) -> str:
        method = read(
            _PaymentMethod, self.client, "GET", f"/payment_methods/{provider_payment_method_id}"
        )
        return method.customer or ""

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        send(
            self.client,
            "POST",
            f"/customers/{provider_customer_id}",
            data=[("invoice_settings[default_payment_method]", provider_payment_method_id)],
        )

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

        The deduplication the protocol relies on lasts
        `METER_EVENT_DEDUPLICATION_HOURS` here, which is the ceiling every retry
        schedule that sends these has to fit under.

        Which refusals are permanent is decided here rather than by the caller,
        because the limits behind them are Stripe's. Only two are: a payload this
        adapter can see is unacceptable whatever happens next, and a timestamp
        that has aged out of the window they accept usage for. Everything else
        Stripe declines is the account rather than the event — most often a meter
        this catalog has not published there yet — and is reported as
        unavailability so the row waits for `publish-catalog` instead of being
        abandoned unsent.
        """

        # Sent rather than defaulted to now: the window this priced closed before
        # the drainer reached it, and letting Stripe stamp arrival time would move
        # usage into whichever period the retry landed in. Both checks happen
        # before the request, because they are the two answers no retry changes.
        timestamp = _epoch(occurred_at, "occurred_at")
        age = utc_now() - occurred_at
        if age > timedelta(days=METER_EVENT_BACKFILL_DAYS):
            raise InvalidInputError(
                f"{identifier} is {age.days} days old and Stripe accepts usage no older "
                f"than {METER_EVENT_BACKFILL_DAYS} days"
            )
        try:
            send(
                self.client,
                "POST",
                "/billing/meter_events",
                data=[
                    ("event_name", event_name),
                    ("identifier", identifier),
                    ("timestamp", timestamp),
                    # The key the meter is configured to read the customer from,
                    # and the only place the provider's own vocabulary appears in
                    # this call.
                    ("payload[stripe_customer_id]", provider_customer_id),
                    ("payload[value]", str(value_nanos)),
                    # Which numbers produced this charge, answerable from the
                    # provider's own copy without joining back to the ledger.
                    ("payload[pricing_version]", pricing_version),
                ],
            )
        except InvalidInputError as refusal:
            raise UpstreamUnavailableError(
                f"Stripe would not take usage on {event_name}: {refusal}"
            ) from refusal

    def create_subscription(
        self, *, provider_customer_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        """Put a customer on a named plan and its metered prices.

        The lines are this package's own published catalog, resolved by lookup
        key rather than by identifier so nothing here stores what Stripe
        generated. Resolving them on the way in is also what makes an unpublished
        catalog fail at the subscribe rather than as a subscription missing a line
        nobody notices until the invoice.

        The convergence the protocol asks for is a read of the customer's live
        subscriptions rather than an idempotency key, which Stripe remembers for
        only a day where the account it protects lasts.
        """

        live = self._live_subscription(provider_customer_id)
        if live is not None:
            return _subscription(live)
        lookup_keys = subscription_price_lookup_keys(plan)
        prices = self._price_ids(lookup_keys)
        fields: list[tuple[str, str]] = [
            ("customer", provider_customer_id),
            # Refuse rather than leave an `incomplete` subscription behind. A
            # first payment that cannot be taken otherwise produces a record that
            # looks subscribed for 23 hours and then expires, and a customer who
            # believes they subscribed and a platform that agrees are the worst
            # possible pair of beliefs about an account with no working card.
            ("payment_behavior", "error_if_incomplete"),
        ]
        # Ordered, because `items[0]`, `items[1]` is the order the lines appear
        # in on the subscription and therefore on the invoice.
        fields.extend(
            (f"items[{index}][price]", prices[key]) for index, key in enumerate(lookup_keys)
        )
        return _subscription(
            read(_Subscription, self.client, "POST", "/subscriptions", data=fields)
        )

    def set_subscription_plan(
        self, *, provider_subscription_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        """Move an existing subscription onto another plan's price.

        The swap is by item id, and whether it is needed at all is read off that
        item's price lookup key — which is what makes a retry after a transaction
        that died converge rather than charge a second proration.

        Prorated and invoiced immediately, refusing rather than leaving an
        unpaid balance behind: a customer who cannot pay for the plan they asked
        for must not end up on it.
        """

        line = plan_line(plan)
        current = read(
            _Subscription, self.client, "GET", f"/subscriptions/{provider_subscription_id}"
        )
        licensed = _plan_item(current)
        if licensed is None:
            raise UpstreamUnavailableError(
                f"Stripe subscription {provider_subscription_id} carries no plan price "
                "this platform published"
            )
        if licensed.price.lookup_key == line.price_lookup_key:
            return _subscription(current)
        price_id = self._price_ids((line.price_lookup_key,))[line.price_lookup_key]
        return _subscription(
            read(
                _Subscription,
                self.client,
                "POST",
                f"/subscriptions/{provider_subscription_id}",
                data=[
                    ("items[0][id]", licensed.id),
                    ("items[0][price]", price_id),
                    ("proration_behavior", "always_invoice"),
                    ("payment_behavior", "error_if_incomplete"),
                ],
            )
        )

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        return _subscription(
            read(_Subscription, self.client, "GET", f"/subscriptions/{provider_subscription_id}")
        )

    def create_credit_grant(
        self,
        *,
        account_id: str,
        provider_customer_id: str,
        amount_nanos: int,
        period_started_at: datetime,
        period_ended_at: datetime,
    ) -> ProviderCreditGrant:
        """Give a customer the usage their plan includes.

        Scoped to metered prices, which is how "spent against usage before
        anything is charged" is stated to Stripe.

        The caller states the period it is buying and this adapter turns that
        into the window Stripe will actually apply the grant in, which is the
        provider's own timing and the only thing this side knows it. Both bounds
        move `CREDIT_GRANT_SETTLEMENT_GRACE` later, because credit is applied
        when an invoice is *finalized* rather than when it is raised: a grant has
        to outlive its own period to reach its invoice, and must not yet be
        spendable when the previous period's invoice finalizes, or an overrun
        there eats this period's allowance.

        Effective no earlier than now, because a grant cannot become spendable
        before it is bought. That is what the shifted start means for a plan
        change, which buys a replacement part-way through a cycle whose start is
        already behind it.

        Stripe grants in cents where everything on this side counts nanodollars,
        so the conversion happens here and refuses a figure it cannot express
        exactly rather than granting a rounded one.

        The idempotency key is derived from the account with the amount and the
        expiry rather than from the account alone, so a replay can never disagree
        with its key — and a plan change, which buys a *different* grant inside
        the same cycle, is not mistaken for the retry of the one it replaces.
        """

        amount_cents = cents(amount_nanos)
        expiry = _epoch(period_ended_at + CREDIT_GRANT_SETTLEMENT_GRACE, "period_ended_at")
        effective = _epoch(
            max(period_started_at + CREDIT_GRANT_SETTLEMENT_GRACE, utc_now()),
            "period_started_at",
        )
        grant = read(
            _CreditGrant,
            self.client,
            "POST",
            "/billing/credit_grants",
            data=[
                ("customer", provider_customer_id),
                ("name", CREDIT_GRANT_NAME),
                # Included with the plan rather than bought, which is what the
                # provider's reporting divides these on.
                ("category", "promotional"),
                ("amount[type]", "monetary"),
                ("amount[monetary][currency]", BILLING_CURRENCY.lower()),
                ("amount[monetary][value]", str(amount_cents)),
                ("applicability_config[scope][price_type]", "metered"),
                ("effective_at", effective),
                ("expires_at", expiry),
            ],
            idempotency_key=(f"{_CREDIT_GRANT_KEY_PREFIX}{account_id}-{amount_cents}-{expiry}"),
        )
        if grant.amount.monetary is None or grant.expires_at is None:
            raise UpstreamUnavailableError(
                "Stripe recorded an allowance without an amount or an expiry"
            )
        return ProviderCreditGrant(
            provider_credit_grant_id=grant.id,
            amount_nanos=grant.amount.monetary.value * NANOS_PER_CENT,
            expires_at=datetime.fromtimestamp(grant.expires_at, tz=UTC),
        )

    def expire_credit_grant(self, *, provider_credit_grant_id: str) -> None:
        """End an allowance now, so nothing further is spent against it.

        Read first, because Stripe refuses to expire a grant that is already over
        and the caller reaching here twice is the ordinary case: a plan change
        that died after expiring the outgoing grant has to converge on retry
        rather than fail on the work it already did.
        """

        grant = read(
            _CreditGrant,
            self.client,
            "GET",
            f"/billing/credit_grants/{provider_credit_grant_id}",
        )
        if grant.voided_at is not None:
            return
        if grant.expires_at is not None and grant.expires_at <= int(utc_now().timestamp()):
            return
        send(
            self.client,
            "POST",
            f"/billing/credit_grants/{provider_credit_grant_id}/expire",
        )

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        """Paged, because an invoice's lines are a list Stripe truncates."""

        totals: dict[str, int] = {}
        event_names: dict[str, str] = {}
        starting_after = ""
        while True:
            params: list[tuple[str, str]] = [
                ("limit", "100"),
                ("expand[]", "data.pricing.price_details.price"),
            ]
            if starting_after:
                params.append(("starting_after", starting_after))
            page = read(
                _InvoiceLines,
                self.client,
                "GET",
                f"/invoices/{provider_invoice_id}/lines",
                params=params,
            )
            for line in page.data:
                meter_id = _meter_id(line)
                if not meter_id:
                    continue
                event_name = event_names.get(meter_id) or self._meter_event_name(meter_id)
                event_names[meter_id] = event_name
                totals[event_name] = totals.get(event_name, 0) + _line_quantity(line)
            if not page.has_more or not page.data:
                return totals
            starting_after = page.data[-1].id

    def _live_subscription(self, provider_customer_id: str) -> _Subscription | None:
        """The subscription this customer is already on, if any.

        Stripe's list omits cancelled subscriptions, so what comes back is what
        is still billing — which is exactly the question "does this customer need
        one". Newest first, and a customer here never holds two.
        """

        listed = read(
            _SubscriptionList,
            self.client,
            "GET",
            "/subscriptions",
            params=[("customer", provider_customer_id), ("limit", "10")],
        )
        return listed.data[0] if listed.data else None

    def _price_ids(self, lookup_keys: Sequence[str]) -> Mapping[str, str]:
        params: FormFields = [
            ("active", "true"),
            ("limit", "100"),
            *(("lookup_keys[]", key) for key in lookup_keys),
        ]
        listed = read(_PriceList, self.client, "GET", "/prices", params=params)
        found = {price.lookup_key: price.id for price in listed.data if price.lookup_key}
        missing = sorted(set(lookup_keys) - found.keys())
        if missing:
            # Not the caller's input to fix and not permanently wrong: publishing
            # the catalog makes the same request succeed unchanged.
            raise UpstreamUnavailableError(
                "Stripe has no active price published for: " + ", ".join(missing)
            )
        return found

    def _meter_event_name(self, meter_id: str) -> str:
        return read(_Meter, self.client, "GET", f"/billing/meters/{meter_id}").event_name


def _subscription(payload: _Subscription) -> ProviderSubscription:
    items = payload.items.data
    if not items:
        raise UpstreamUnavailableError(f"Stripe returned subscription {payload.id} with no items")
    licensed = _plan_item(payload)
    # The billing period belongs to the items rather than to the subscription.
    # Every item created together shares one cycle, so its span across them is
    # that cycle whatever order they came back in.
    return ProviderSubscription(
        provider_subscription_id=payload.id,
        status=payload.status,
        current_period_started_at=datetime.fromtimestamp(
            min(item.current_period_start for item in items), tz=UTC
        ),
        current_period_ended_at=datetime.fromtimestamp(
            max(item.current_period_end for item in items), tz=UTC
        ),
        plan=plan_for_price_lookup_key(licensed.price.lookup_key or "")
        if licensed is not None
        else None,
    )


def _plan_item(payload: _Subscription) -> _SubscriptionItem | None:
    """The one item carrying a plan price this platform published.

    `None` where no item does, which is a subscription somebody assembled
    elsewhere on the same account. Nothing here decides anything about one of
    those: the allowance it would carry and the grant it would be given are both
    the plan's.
    """

    for item in payload.items.data:
        if plan_for_price_lookup_key(item.price.lookup_key or "") is not None:
            return item
    return None


def _meter_id(line: _InvoiceLine) -> str:
    if line.pricing is None or line.pricing.price_details.price.recurring is None:
        return ""
    return line.pricing.price_details.price.recurring.meter or ""


def _line_quantity(line: _InvoiceLine) -> int:
    if line.quantity_decimal is None:
        raise UpstreamUnavailableError(f"Stripe billed line {line.id} without a quantity")
    try:
        quantity = Decimal(line.quantity_decimal)
    except InvalidOperation as exc:
        raise UpstreamUnavailableError(
            f"Stripe billed line {line.id} a quantity that is not a number"
        ) from exc
    if quantity != quantity.to_integral_value():
        # Meter values leave here as integer nanodollars and are summed, so a
        # fractional total means the invoice was built from something other than
        # what was sent — which is exactly what this comparison exists to catch.
        raise UpstreamUnavailableError(
            f"Stripe billed line {line.id} a fractional count of nanodollars"
        )
    return int(quantity)


def _epoch(value: datetime, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        # A naive datetime would be read as this process's local time, which puts
        # usage in the wrong period on any host that is not UTC and puts it there
        # silently.
        raise InvalidInputError(f"{field} must carry a timezone")
    return str(int(value.timestamp()))


__all__ = [
    "CREDIT_GRANT_NAME",
    "METER_EVENT_BACKFILL_DAYS",
    "METER_EVENT_DEDUPLICATION_HOURS",
    "StripeBilling",
]
