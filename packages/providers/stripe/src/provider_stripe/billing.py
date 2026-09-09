from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import Field
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import published_plan, subscription_terms
from shared.credit_payments import CreditPayment, CreditPaymentStatus, CreditPurchaseCheckout
from shared.errors import InvalidInputError, PaymentRequiredError, UpstreamUnavailableError
from shared.payments import (
    BILLING_CURRENCY,
    HostedPaymentSession,
    PaymentCustomer,
    ProviderInvoice,
    ProviderPaidSubscriptionPeriod,
    ProviderSubscription,
    SubscriptionChangeTiming,
)
from shared.timestamps import to_utc, utc_now

from provider_stripe.api import FormFields, StripeObject, read, send
from provider_stripe.catalog import (
    NANOS_PER_CENT,
    PLAN_LINES,
    cents,
    plan_line,
    terms_for_price_lookup_key,
)

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


class _Customer(StripeObject):
    id: str = Field(min_length=1)


class _CreditCheckout(StripeObject):
    id: str
    customer: str
    client_reference_id: str
    payment_intent: str | None
    url: str | None
    expires_at: int
    status: str


class _InvoicePaymentSettings(StripeObject):
    default_payment_method: str | None


class _CreditCustomer(StripeObject):
    invoice_settings: _InvoicePaymentSettings


class _CreditCharge(StripeObject):
    id: str
    payment_intent: str
    amount_captured: int = Field(ge=0, strict=True)
    amount_refunded: int = Field(ge=0, strict=True)
    paid: bool
    captured: bool
    disputed: bool


class _CreditPaymentError(StripeObject):
    code: str = ""


class _CreditIntent(StripeObject):
    id: str
    customer: str
    currency: str
    amount: int = Field(gt=0, strict=True)
    amount_received: int = Field(ge=0, strict=True)
    status: str
    metadata: dict[str, str]
    latest_charge: _CreditCharge | None
    last_payment_error: _CreditPaymentError | None = None


class _CreditDispute(StripeObject):
    id: str
    charge: str
    currency: str
    amount: int = Field(gt=0, strict=True)
    status: str


class _CreditDisputes(StripeObject):
    data: list[_CreditDispute]
    has_more: bool


class _HostedSession(StripeObject):
    url: str = ""


class _PaymentMethod(StripeObject):
    customer: str | None = None


class _PaymentMethodList(StripeObject):
    data: list[_PaymentMethod] = Field(default_factory=list)


class _Recurring(StripeObject):
    meter: str | None = None
    interval: str
    interval_count: int
    usage_type: str


class _Price(StripeObject):
    """The parts of a price this package reads, wherever one is carried.

    One shape for all three places Stripe returns a price — a subscription item,
    a listing, an invoice line — because each carries the same object and naming
    it three times is three descriptions of one thing to keep in step.
    """

    id: str
    lookup_key: str | None = None
    product: str = ""
    currency: str
    unit_amount: int | None = None
    recurring: _Recurring | None = None


class _SubscriptionItem(StripeObject):
    id: str
    price: _Price
    current_period_start: int
    current_period_end: int
    quantity: int | None = None


class _SubscriptionItems(StripeObject):
    data: list[_SubscriptionItem] = Field(default_factory=list)


class _Subscription(StripeObject):
    id: str
    status: str
    items: _SubscriptionItems
    customer: str = ""
    schedule: str | None = None


class _SchedulePhaseItem(StripeObject):
    price: _Price
    quantity: int | None = None


class _ScheduleDiscount(StripeObject):
    discount: str | None = None
    coupon: str | None = None
    promotion_code: str | None = None


class _SchedulePhase(StripeObject):
    start_date: int
    end_date: int
    items: list[_SchedulePhaseItem]
    metadata: dict[str, str] = Field(default_factory=dict)
    discounts: list[_ScheduleDiscount] | None = None
    default_tax_rates: list[str] = Field(default_factory=list)


class _CreatedSchedule(StripeObject):
    id: str
    subscription: str | None
    status: str


class _SubscriptionSchedule(_CreatedSchedule):
    phases: list[_SchedulePhase]
    metadata: dict[str, str] = Field(default_factory=dict)


class _SubscriptionList(StripeObject):
    data: list[_Subscription] = Field(default_factory=list)


class _PriceList(StripeObject):
    data: list[_Price] = Field(default_factory=list)


class _LinePeriod(StripeObject):
    start: int
    end: int


class _SubscriptionLineParent(StripeObject):
    subscription: str | None
    proration: bool


class _LineParent(StripeObject):
    type: str
    subscription_item_details: _SubscriptionLineParent | None = None


class _PriceDetails(StripeObject):
    price: _Price


class _LinePricing(StripeObject):
    price_details: _PriceDetails


class _InvoiceLine(StripeObject):
    id: str
    quantity_decimal: str | None = None
    pricing: _LinePricing | None = None
    parent: _LineParent | None = None
    period: _LinePeriod | None = None
    amount: int | None = None
    currency: str = ""


class _InvoiceLines(StripeObject):
    data: list[_InvoiceLine] = Field(default_factory=list)
    has_more: bool = False


class _Invoice(StripeObject):
    id: str
    status: str = ""
    period_start: int
    period_end: int
    customer: str
    currency: str
    amount_paid: int
    status_transitions: _InvoiceStatusTransitions


class _InvoiceStatusTransitions(StripeObject):
    paid_at: int | None = None


class _InvoiceList(StripeObject):
    data: list[_Invoice] = Field(default_factory=list)
    has_more: bool


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

    def create_credit_purchase_checkout(
        self,
        *,
        provider_customer_id: str,
        purchase_id: str,
        amount_nanos: int,
        success_url: str,
        cancel_url: str,
    ) -> CreditPurchaseCheckout:
        amount_cents = cents(amount_nanos)
        if amount_cents <= 0:
            raise InvalidInputError("credit purchases must have a positive amount")
        session = read(
            _CreditCheckout,
            self.client,
            "POST",
            "/checkout/sessions",
            data=[
                ("mode", "payment"),
                ("customer", provider_customer_id),
                ("client_reference_id", purchase_id),
                ("metadata[credit_purchase_id]", purchase_id),
                ("payment_intent_data[metadata][credit_purchase_id]", purchase_id),
                ("payment_method_types[0]", "card"),
                ("line_items[0][price_data][currency]", BILLING_CURRENCY.lower()),
                ("line_items[0][price_data][unit_amount]", str(amount_cents)),
                ("line_items[0][price_data][product_data][name]", "Compute and storage credit"),
                ("line_items[0][quantity]", "1"),
                ("success_url", success_url),
                ("cancel_url", cancel_url),
            ],
            idempotency_key=f"credit-checkout-{purchase_id}",
        )
        return _credit_checkout(session)

    def credit_purchase_checkout(self, *, provider_session_id: str) -> CreditPurchaseCheckout:
        return _credit_checkout(
            read(_CreditCheckout, self.client, "GET", f"/checkout/sessions/{provider_session_id}")
        )

    def create_credit_purchase_payment(
        self, *, provider_customer_id: str, purchase_id: str, amount_nanos: int
    ) -> CreditPayment:
        amount_cents = cents(amount_nanos)
        if amount_cents <= 0:
            raise InvalidInputError("credit purchases must have a positive amount")
        customer = read(_CreditCustomer, self.client, "GET", f"/customers/{provider_customer_id}")
        payment_method = customer.invoice_settings.default_payment_method
        if payment_method is None:
            raise PaymentRequiredError("save a default payment method before automatic reload")
        intent = read(
            _CreditIntent,
            self.client,
            "POST",
            "/payment_intents",
            data=[
                ("customer", provider_customer_id),
                ("amount", str(amount_cents)),
                ("currency", BILLING_CURRENCY.lower()),
                ("payment_method", payment_method),
                ("payment_method_types[0]", "card"),
                ("capture_method", "automatic"),
                ("metadata[credit_purchase_id]", purchase_id),
                ("expand[]", "latest_charge"),
            ],
            idempotency_key=f"credit-payment-{purchase_id}",
        )
        return self._credit_payment(intent)

    def confirm_credit_purchase_payment(self, *, provider_payment_id: str) -> CreditPayment:
        try:
            send(
                self.client,
                "POST",
                f"/payment_intents/{provider_payment_id}/confirm",
                data=[("off_session", "true")],
                idempotency_key=f"credit-confirm-{provider_payment_id}",
            )
        except InvalidInputError:
            # A declined card is an HTTP error; the persisted intent tells the
            # domain whether to ask for authentication or a different card.
            result = self.credit_purchase_payment(provider_payment_id=provider_payment_id)
            if result.status not in {
                CreditPaymentStatus.Declined,
                CreditPaymentStatus.ActionRequired,
                CreditPaymentStatus.Succeeded,
            }:
                raise
            return result
        return self.credit_purchase_payment(provider_payment_id=provider_payment_id)

    def cancel_credit_purchase_payment(self, *, provider_payment_id: str) -> CreditPayment:
        try:
            send(
                self.client,
                "POST",
                f"/payment_intents/{provider_payment_id}/cancel",
                data=[("cancellation_reason", "abandoned")],
                idempotency_key=f"credit-cancel-{provider_payment_id}",
            )
        except InvalidInputError:
            result = self.credit_purchase_payment(provider_payment_id=provider_payment_id)
            if result.status not in {CreditPaymentStatus.Cancelled, CreditPaymentStatus.Succeeded}:
                raise
            return result
        return self.credit_purchase_payment(provider_payment_id=provider_payment_id)

    def credit_purchase_payment(self, *, provider_payment_id: str) -> CreditPayment:
        intent = read(
            _CreditIntent,
            self.client,
            "GET",
            f"/payment_intents/{provider_payment_id}",
            params=[("expand[]", "latest_charge")],
        )
        return self._credit_payment(intent)

    def _credit_payment(self, intent: _CreditIntent) -> CreditPayment:
        statuses = {
            "requires_confirmation": CreditPaymentStatus.Pending,
            "processing": CreditPaymentStatus.Pending,
            "requires_capture": CreditPaymentStatus.Pending,
            "requires_payment_method": CreditPaymentStatus.Declined,
            "requires_action": CreditPaymentStatus.ActionRequired,
            "canceled": CreditPaymentStatus.Cancelled,
            "succeeded": CreditPaymentStatus.Succeeded,
        }
        status = statuses.get(intent.status)
        if (
            status is CreditPaymentStatus.Declined
            and intent.last_payment_error is not None
            and intent.last_payment_error.code == "authentication_required"
        ):
            status = CreditPaymentStatus.ActionRequired
        purchase_id = intent.metadata.get("credit_purchase_id", "")
        if intent.currency.upper() != BILLING_CURRENCY or status is None or not purchase_id:
            raise UpstreamUnavailableError("Stripe payment lacks valid credit purchase evidence")
        charge = intent.latest_charge
        if charge is not None and charge.payment_intent != intent.id:
            raise UpstreamUnavailableError("Stripe charge does not belong to the credit payment")
        if status is CreditPaymentStatus.Succeeded and (
            charge is None
            or not charge.paid
            or not charge.captured
            or charge.amount_captured != intent.amount_received
        ):
            raise UpstreamUnavailableError("Stripe credit payment lacks a matching captured charge")
        disputed = 0
        if charge is not None and charge.disputed:
            cursor = ""
            seen: set[str] = set()
            while True:
                params = [("charge", charge.id), ("limit", "100")]
                if cursor:
                    params.append(("starting_after", cursor))
                page = read(_CreditDisputes, self.client, "GET", "/disputes", params=params)
                for dispute in page.data:
                    if (
                        dispute.id in seen
                        or dispute.charge != charge.id
                        or dispute.currency.upper() != BILLING_CURRENCY
                    ):
                        raise UpstreamUnavailableError("Stripe dispute evidence does not reconcile")
                    seen.add(dispute.id)
                    if dispute.status in {"needs_response", "under_review", "lost"}:
                        disputed += dispute.amount
                    elif dispute.status not in {
                        "won",
                        "warning_needs_response",
                        "warning_under_review",
                        "warning_closed",
                    }:
                        raise UpstreamUnavailableError("Stripe returned an unknown dispute status")
                if not page.has_more:
                    break
                if not page.data:
                    raise UpstreamUnavailableError("Stripe dispute pagination made no progress")
                cursor = page.data[-1].id
            if not seen:
                raise UpstreamUnavailableError("Stripe disputed charge has no dispute evidence")
        return CreditPayment(
            provider_payment_id=intent.id,
            provider_customer_id=intent.customer,
            purchase_id=purchase_id,
            status=status,
            amount_nanos=intent.amount * NANOS_PER_CENT,
            received_nanos=intent.amount_received * NANOS_PER_CENT,
            refunded_nanos=(charge.amount_refunded if charge else 0) * NANOS_PER_CENT,
            disputed_nanos=disputed * NANOS_PER_CENT,
            confirmation_required=intent.status == "requires_confirmation",
        )

    def create_customer(self, *, account_id: str, email: str, workspace_id: str) -> PaymentCustomer:
        """Register a payer using the account's idempotency key.

        Persist the customer ID before Stripe's 24-hour idempotency window expires.
        """

        data = [
            # Associate provider-side payment investigations with the workspace.
            ("metadata[workspace_id]", workspace_id),
        ]
        # Identity providers may omit a verified email address.
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

    def has_payment_method(self, *, provider_customer_id: str) -> bool:
        """Whether Stripe holds anything chargeable for this customer.

        One page of one is the whole question: the caller asks whether there is
        any instrument, never which or how many, and Stripe orders the list
        newest first so a customer with a hundred cards costs the same read as a
        customer with one.

        Deliberately not `invoice_settings.default_payment_method`. That names
        the card charges are *taken from*, which is empty for a customer who
        saved a card in the window before the delivery that promotes it lands —
        and treating that customer as having none would take back an allowance
        they had already been given.
        """

        methods = read(
            _PaymentMethodList,
            self.client,
            "GET",
            "/payment_methods",
            params=[("customer", provider_customer_id), ("limit", "1")],
        )
        return bool(methods.data)

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
        """Subscribe to the licensed plan, reusing an existing live subscription."""

        live = self._live_subscription(provider_customer_id)
        if live is not None:
            return self._subscription_state(live)
        line = plan_line(published_plan(plan).terms_version)
        price_id = self._price_ids((line.price_lookup_key,))[line.price_lookup_key]
        fields = [
            ("customer", provider_customer_id),
            ("payment_behavior", "error_if_incomplete"),
            ("billing_mode[type]", "flexible"),
            ("items[0][price]", price_id),
        ]
        return self._subscription_state(
            read(_Subscription, self.client, "POST", "/subscriptions", data=fields)
        )

    def set_subscription_plan(
        self,
        *,
        provider_subscription_id: str,
        terms_version: SubscriptionTermsVersion,
        timing: SubscriptionChangeTiming,
        operation_id: str,
        operation_created_at: datetime,
    ) -> ProviderSubscription:
        current = read(
            _Subscription, self.client, "GET", f"/subscriptions/{provider_subscription_id}"
        )
        licensed = _plan_item(current)
        if licensed is None:
            raise UpstreamUnavailableError("the subscription has no recognized licensed price")
        held_version = _terms_for_price(licensed.price)
        if held_version == terms_version:
            if current.schedule:
                schedule = self._read_schedule(current.schedule)
                self._require_owned_schedule(schedule, current.id)
                read(
                    _CreatedSchedule,
                    self.client,
                    "POST",
                    f"/subscription_schedules/{schedule.id}/release",
                )
            return self.subscription(provider_subscription_id=current.id)
        if current.status not in {"active", "trialing", "past_due", "unpaid"}:
            return self._subscription_state(current)
        if timing is SubscriptionChangeTiming.Immediate and to_utc(utc_now()) - to_utc(
            operation_created_at
        ) >= timedelta(hours=20):
            raise UpstreamUnavailableError(
                "unconfirmed subscription upgrade exceeded its retry window"
            )
        if timing is SubscriptionChangeTiming.AtRenewal:
            return self._schedule_plan_change(
                current,
                terms_version=terms_version,
                operation_id=operation_id,
                operation_created_at=operation_created_at,
            )
        if current.schedule:
            schedule = self._read_schedule(current.schedule)
            self._require_owned_schedule(schedule, current.id)
            read(
                _CreatedSchedule,
                self.client,
                "POST",
                f"/subscription_schedules/{schedule.id}/release",
            )
        line = plan_line(terms_version)
        price_id = self._price_ids((line.price_lookup_key,))[line.price_lookup_key]
        changed = read(
            _Subscription,
            self.client,
            "POST",
            f"/subscriptions/{current.id}",
            data=[
                ("items[0][id]", licensed.id),
                ("items[0][price]", price_id),
                ("proration_behavior", "always_invoice"),
                ("payment_behavior", "error_if_incomplete"),
            ],
            idempotency_key=f"subscription-change-{operation_id}",
        )
        return self._subscription_state(changed)

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        return self._subscription_state(
            read(_Subscription, self.client, "GET", f"/subscriptions/{provider_subscription_id}")
        )

    def _read_schedule(self, schedule_id: str) -> _SubscriptionSchedule:
        return read(
            _SubscriptionSchedule,
            self.client,
            "GET",
            f"/subscription_schedules/{schedule_id}",
            params=[("expand[]", "phases.items.price")],
        )

    @staticmethod
    def _require_owned_schedule(schedule: _SubscriptionSchedule, subscription_id: str) -> None:
        if (
            schedule.subscription != subscription_id
            or schedule.metadata.get("lazycloud_subscription") != subscription_id
            or schedule.status not in {"active", "not_started"}
        ):
            raise UpstreamUnavailableError(
                "subscription schedule ownership requires reconciliation"
            )

    def _subscription_state(self, payload: _Subscription) -> ProviderSubscription:
        result = _subscription(payload)
        if payload.schedule is None:
            return result
        schedule = self._read_schedule(payload.schedule)
        self._require_owned_schedule(schedule, payload.id)
        future = [
            phase
            for phase in schedule.phases
            if phase.start_date >= int(result.current_period_ended_at.timestamp())
        ]
        if not future:
            return result
        if len(future) != 1 or future[0].start_date != int(
            result.current_period_ended_at.timestamp()
        ):
            raise UpstreamUnavailableError("subscription schedule has an unexpected future phase")
        versions = [
            version
            for item in future[0].items
            if (version := _terms_for_price(item.price)) is not None
        ]
        if len(versions) != 1:
            raise UpstreamUnavailableError("scheduled subscription terms are ambiguous")
        current_usage = sorted(
            (item.price.id, item.quantity)
            for item in payload.items.data
            if _terms_for_price(item.price) is None
        )
        scheduled_usage = sorted(
            (item.price.id, item.quantity)
            for item in future[0].items
            if _terms_for_price(item.price) is None
        )
        if current_usage != scheduled_usage:
            raise UpstreamUnavailableError("subscription schedule changes its metered items")
        return result.model_copy(
            update={
                "scheduled_terms_version": versions[0],
                "scheduled_change_at": datetime.fromtimestamp(future[0].start_date, tz=UTC),
            }
        )

    def _schedule_plan_change(
        self,
        current: _Subscription,
        *,
        terms_version: SubscriptionTermsVersion,
        operation_id: str,
        operation_created_at: datetime,
    ) -> ProviderSubscription:
        schedule = self._read_schedule(current.schedule) if current.schedule else None
        if schedule is None or schedule.metadata.get("lazycloud_subscription") != current.id:
            if to_utc(utc_now()) - to_utc(operation_created_at) >= timedelta(hours=20):
                raise UpstreamUnavailableError(
                    "unconfirmed subscription schedule creation exceeded its retry window"
                )
            created = read(
                _CreatedSchedule,
                self.client,
                "POST",
                "/subscription_schedules",
                data=[("from_subscription", current.id)],
                idempotency_key=f"subscription-schedule-{operation_id}",
            )
            if (
                created.subscription != current.id
                or created.status != "active"
                or (current.schedule is not None and created.id != current.schedule)
            ):
                raise UpstreamUnavailableError(
                    "created subscription schedule does not match the pending operation"
                )
            schedule = self._read_schedule(created.id)
        else:
            self._require_owned_schedule(schedule, current.id)
        held = _subscription(current)
        phases = [
            phase
            for phase in schedule.phases
            if phase.start_date <= int(held.current_period_started_at.timestamp()) < phase.end_date
        ]
        if len(phases) != 1:
            raise UpstreamUnavailableError("subscription schedule has no unique current phase")
        phase = phases[0]
        line = plan_line(terms_version)
        price_id = self._price_ids((line.price_lookup_key,))[line.price_lookup_key]
        fields = [
            ("end_behavior", "release"),
            ("proration_behavior", "none"),
            ("metadata[lazycloud_subscription]", current.id),
            ("phases[0][start_date]", str(phase.start_date)),
            ("phases[0][end_date]", str(int(held.current_period_ended_at.timestamp()))),
            ("phases[0][proration_behavior]", "none"),
            ("phases[1][duration][interval]", "month"),
            ("phases[1][duration][interval_count]", "1"),
            ("phases[1][proration_behavior]", "none"),
        ]
        for phase_index in (0, 1):
            for index, item in enumerate(phase.items):
                price = (
                    price_id
                    if phase_index == 1 and _terms_for_price(item.price) is not None
                    else item.price.id
                )
                fields.append((f"phases[{phase_index}][items][{index}][price]", price))
                if item.quantity is not None:
                    fields.append(
                        (f"phases[{phase_index}][items][{index}][quantity]", str(item.quantity))
                    )
            for key, value in phase.metadata.items():
                fields.append((f"phases[{phase_index}][metadata][{key}]", value))
            for index, rate in enumerate(phase.default_tax_rates):
                fields.append((f"phases[{phase_index}][default_tax_rates][{index}]", rate))
            for index, discount in enumerate(phase.discounts or ()):
                for key, value in (
                    ("discount", discount.discount),
                    ("coupon", discount.coupon),
                    ("promotion_code", discount.promotion_code),
                ):
                    if value:
                        fields.append((f"phases[{phase_index}][discounts][{index}][{key}]", value))
        read(
            _CreatedSchedule,
            self.client,
            "POST",
            f"/subscription_schedules/{schedule.id}",
            data=fields,
            idempotency_key=f"subscription-schedule-phases-{operation_id}",
        )
        verified = self.subscription(provider_subscription_id=current.id)
        if verified.scheduled_terms_version != terms_version:
            raise UpstreamUnavailableError(
                "subscription schedule does not hold the requested terms"
            )
        return verified

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        """Paged, because an invoice's lines are a list Stripe truncates."""

        totals: dict[str, int] = {}
        event_names: dict[str, str] = {}
        for line in self._invoice_lines(provider_invoice_id):
            meter_id = _meter_id(line)
            if not meter_id:
                continue
            event_name = event_names.get(meter_id) or self._meter_event_name(meter_id)
            event_names[meter_id] = event_name
            totals[event_name] = totals.get(event_name, 0) + _line_quantity(line)
        return totals

    def _invoice_lines(self, provider_invoice_id: str) -> Iterator[_InvoiceLine]:
        starting_after = ""
        seen: set[str] = set()
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
            starting_after = _page_cursor([line.id for line in page.data], page.has_more, seen)
            yield from page.data
            if not page.has_more:
                return

    def invoices_for(
        self, *, provider_customer_id: str, since: datetime, limit: int | None = 12
    ) -> Sequence[ProviderInvoice]:
        """List invoices newest first; None exhausts the requested history."""
        return [
            ProviderInvoice(
                provider_invoice_id=invoice.id,
                status=invoice.status,
                period_started_at=datetime.fromtimestamp(invoice.period_start, tz=UTC),
                period_ended_at=datetime.fromtimestamp(invoice.period_end, tz=UTC),
            )
            for invoice in self._invoices(provider_customer_id, since=since, limit=limit)
        ]

    def _invoices(
        self, provider_customer_id: str, *, since: datetime, limit: int | None
    ) -> Iterator[_Invoice]:
        if limit is not None and limit <= 0:
            raise ValueError("invoice limit must be positive")
        starting_after = ""
        seen: set[str] = set()
        count = 0
        while True:
            params = [
                ("customer", provider_customer_id),
                ("created[gte]", _epoch(since, "since")),
                ("limit", str(min(limit - count, 100) if limit is not None else 100)),
            ]
            if starting_after:
                params.append(("starting_after", starting_after))
            page = read(_InvoiceList, self.client, "GET", "/invoices", params=params)
            starting_after = _page_cursor(
                [invoice.id for invoice in page.data], page.has_more, seen
            )
            for invoice in page.data:
                if invoice.customer != provider_customer_id or not invoice.status:
                    raise UpstreamUnavailableError("Stripe invoice ownership or status is missing")
                yield invoice
                count += 1
                if limit is not None and count >= limit:
                    return
            if not page.has_more:
                return

    def paid_subscription_periods(
        self,
        *,
        provider_customer_id: str,
        provider_subscription_id: str,
        since: datetime,
    ) -> Sequence[ProviderPaidSubscriptionPeriod]:
        subscription = read(
            _Subscription, self.client, "GET", f"/subscriptions/{provider_subscription_id}"
        )
        if (
            subscription.customer != provider_customer_id
            or _subscription(subscription).plan is None
        ):
            raise UpstreamUnavailableError("Stripe subscription has no matching customer and plan")
        periods: list[ProviderPaidSubscriptionPeriod] = []
        for invoice in self._invoices(provider_customer_id, since=since, limit=None):
            if invoice.status != "paid":
                continue
            if (
                invoice.currency != BILLING_CURRENCY.lower()
                or invoice.status_transitions.paid_at is None
            ):
                raise UpstreamUnavailableError("Stripe paid invoice has no USD payment evidence")
            for line in self._invoice_lines(invoice.id):
                parent = line.parent
                if parent is None or parent.type != "subscription_item_details":
                    continue
                details = parent.subscription_item_details
                if details is None or details.subscription != provider_subscription_id:
                    continue
                version = (
                    _terms_for_price(line.pricing.price_details.price) if line.pricing else None
                )
                if version is None:
                    continue
                if (
                    line.period is None
                    or line.amount is None
                    or line.currency != BILLING_CURRENCY.lower()
                ):
                    raise UpstreamUnavailableError(
                        "Stripe paid plan line has incomplete billing evidence"
                    )
                if line.period.end <= line.period.start:
                    raise UpstreamUnavailableError(
                        "Stripe paid plan line has an invalid billing period"
                    )
                periods.append(
                    ProviderPaidSubscriptionPeriod(
                        provider_invoice_id=invoice.id,
                        provider_invoice_line_id=line.id,
                        provider_subscription_id=provider_subscription_id,
                        plan=subscription_terms(version).plan,
                        terms_version=version,
                        period_started_at=datetime.fromtimestamp(line.period.start, tz=UTC),
                        period_ended_at=datetime.fromtimestamp(line.period.end, tz=UTC),
                        prorated=details.proration,
                        amount_nanos=line.amount * NANOS_PER_CENT,
                        invoice_paid_nanos=invoice.amount_paid * NANOS_PER_CENT,
                        paid_at=datetime.fromtimestamp(invoice.status_transitions.paid_at, tz=UTC),
                    )
                )
        return periods

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


def _page_cursor(ids: list[str], has_more: bool, seen: set[str]) -> str:
    if (has_more and not ids) or len(set(ids)) != len(ids) or seen.intersection(ids):
        raise UpstreamUnavailableError("Stripe returned an incomplete or repeated history page")
    seen.update(ids)
    return ids[-1] if ids else ""


def _credit_checkout(session: _CreditCheckout) -> CreditPurchaseCheckout:
    if session.status not in {"open", "complete", "expired"}:
        raise UpstreamUnavailableError("Stripe returned an unknown checkout status")
    return CreditPurchaseCheckout(
        provider_session_id=session.id,
        provider_customer_id=session.customer,
        purchase_id=session.client_reference_id,
        provider_payment_id=session.payment_intent or "",
        url=session.url,
        expires_at=datetime.fromtimestamp(session.expires_at, UTC),
        expired=session.status == "expired",
    )


def _terms_for_price(price: _Price) -> SubscriptionTermsVersion | None:
    line = terms_for_price_lookup_key(price.lookup_key or "")
    if line is None:
        if any(item.product_id == price.product for item in PLAN_LINES):
            raise UpstreamUnavailableError(
                f"Stripe price {price.id} has unknown subscription terms"
            )
        return None
    terms = subscription_terms(line.terms_version)
    recurring = price.recurring
    if (
        price.product != line.product_id
        or price.currency != BILLING_CURRENCY.lower()
        or price.unit_amount != terms.monthly_nanos // NANOS_PER_CENT
        or recurring is None
        or recurring.interval != "month"
        or recurring.interval_count != 1
        or recurring.usage_type != "licensed"
    ):
        raise UpstreamUnavailableError(
            f"Stripe price {price.id} disagrees with its subscription terms"
        )
    return line.terms_version


def _subscription(payload: _Subscription) -> ProviderSubscription:
    items = payload.items.data
    if not items:
        raise UpstreamUnavailableError(f"Stripe returned subscription {payload.id} with no items")
    licensed = _plan_item(payload)
    # The billing period belongs to the items rather than to the subscription.
    # Every item created together shares one cycle, so its span across them is
    # that cycle whatever order they came back in.
    version = _terms_for_price(licensed.price) if licensed is not None else None
    return ProviderSubscription(
        provider_subscription_id=payload.id,
        status=payload.status,
        current_period_started_at=datetime.fromtimestamp(
            min(item.current_period_start for item in items), tz=UTC
        ),
        current_period_ended_at=datetime.fromtimestamp(
            max(item.current_period_end for item in items), tz=UTC
        ),
        plan=subscription_terms(version).plan if version is not None else None,
        terms_version=version,
        scheduled_terms_version=None,
        scheduled_change_at=None,
    )


def _plan_item(payload: _Subscription) -> _SubscriptionItem | None:
    """The one item carrying a plan price this platform published.

    `None` where no item does, which is a subscription somebody assembled
    elsewhere on the same account. Nothing here decides anything about one of
    those: the allowance it would carry and the grant it would be given are both
    the plan's.
    """

    for item in payload.items.data:
        if _terms_for_price(item.price) is not None:
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
    "METER_EVENT_BACKFILL_DAYS",
    "METER_EVENT_DEDUPLICATION_HOURS",
    "StripeBilling",
]
