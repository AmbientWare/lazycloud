from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from api.server.services import ApiServices
from billing.invoices import payment_outcome
from billing.jobs import closable_month
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_periods import BillingPeriodRepository
from pydantic import JsonValue
from shared.billing_accounts import BillingAccountStatus, BillingPlan
from shared.billing_periods import BillingPeriodStatus
from shared.payments import (
    HostedPaymentSession,
    InvoiceLine,
    PaymentCustomer,
    PaymentEvent,
    ProviderInvoice,
)
from shared.usage import UsageMetric, UsageRecord, UsageUnit
from tests.service_fixtures import workspace_owner_user_id

from billing import BillingCloseJob, BillingDailyJob, BillingWebhookService

_USAGE_DAY = datetime(2026, 9, 15, 12, tzinfo=UTC)
_CLOSE_RUN = datetime(2026, 10, 3, 6, tzinfo=UTC)


@dataclass(slots=True)
class _Provider:
    """A payment provider whose invoices can be made to say anything.

    The outcome is set on the provider rather than carried in the call, because
    that is where it comes from in production: a delivery says only that
    something changed, and the invoice is read back to find out what.
    """

    drafts: dict[str, list[InvoiceLine]] = field(default_factory=dict)
    finalized: list[str] = field(default_factory=list)
    period_keys: dict[str, str] = field(default_factory=dict)
    status: str = "open"
    attempted: bool = False
    card_pays: bool = False
    """False here: these tests drive outcomes through the webhook, so the charge
    at close must leave the invoice exactly as the test set it."""
    default_payment_methods: dict[str, str] = field(default_factory=dict)
    payment_method_owners: dict[str, str] = field(default_factory=dict)
    """Which customer each saved card currently belongs to, as the provider would
    answer it — the read-back that stops a retried notification about a replaced
    card from putting the old one back."""

    def create_customer(self, *, email: str, workspace_id: str) -> PaymentCustomer:
        raise AssertionError("settling a period must not create customers")

    def draft_invoice(self, *, provider_customer_id: str, period_key: str) -> ProviderInvoice:
        for invoice_id, key in self.period_keys.items():
            if key == period_key:
                return self.fetch_invoice(provider_invoice_id=invoice_id)
        invoice_id = f"in_{len(self.drafts) + 1}"
        self.drafts[invoice_id] = []
        self.period_keys[invoice_id] = period_key
        return ProviderInvoice(provider_invoice_id=invoice_id, total_cents=0, status="draft")

    def replace_invoice_lines(
        self,
        *,
        provider_invoice_id: str,
        provider_customer_id: str,
        currency: str,
        lines: tuple[InvoiceLine, ...],
    ) -> None:
        self.drafts[provider_invoice_id] = list(lines)

    def finalize_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        self.finalized.append(provider_invoice_id)
        return self.fetch_invoice(provider_invoice_id=provider_invoice_id)

    def card_setup_session(
        self, *, provider_customer_id: str, currency: str, success_url: str, cancel_url: str
    ) -> HostedPaymentSession:
        return HostedPaymentSession(url=f"https://payments.test/setup/{provider_customer_id}")

    def customer_portal_session(
        self, *, provider_customer_id: str, return_url: str
    ) -> HostedPaymentSession:
        return HostedPaymentSession(url=f"https://payments.test/portal/{provider_customer_id}")

    def payment_method_owner(self, *, provider_payment_method_id: str) -> str:
        return self.payment_method_owners.get(provider_payment_method_id, "")

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        self.default_payment_methods[provider_customer_id] = provider_payment_method_id

    def pay_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        self.attempted = True
        if self.card_pays:
            self.status = "paid"
        return self.fetch_invoice(provider_invoice_id=provider_invoice_id)

    def fetch_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        total = sum(line.amount_cents for line in self.drafts[provider_invoice_id])
        status = self.status if provider_invoice_id in self.finalized else "draft"
        return ProviderInvoice(
            provider_invoice_id=provider_invoice_id,
            total_cents=total,
            status=status,
            paid=status == "paid",
            attempted=self.attempted,
        )


def test_a_payment_outcome_settles_the_period_and_is_applied_only_once(
    isolated_services: ApiServices,
) -> None:
    """A retried delivery must not apply its outcome a second time.

    The provider retries anything it does not get a 2xx for, and retries again if
    the acknowledgement is lost coming back — so one payment arriving twice is
    ordinary traffic. Applied twice it would move an account's standing twice,
    and the second move has no payment behind it.
    """

    provider, period = _invoiced_period(isolated_services)
    provider.status = "paid"

    with isolated_services.context.database.session() as session:
        first = BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_paid_1", type="invoice.paid", object_id=period.provider_invoice_id
            )
        )
        session.commit()
    with isolated_services.context.database.session() as session:
        again = BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_paid_1", type="invoice.paid", object_id=period.provider_invoice_id
            )
        )
        session.commit()

    assert first
    assert not again
    with isolated_services.context.database.session() as session:
        settled = BillingPeriodRepository(session).get(
            user_id=period.user_id, period_start=period.period_start
        )
        account = BillingAccountRepository(session).get_by_user(period.user_id)
    assert settled is not None
    assert settled.status is BillingPeriodStatus.Paid
    assert account is not None
    assert account.status is BillingAccountStatus.Active


def test_a_payment_that_arrives_after_a_failure_clears_the_account(
    isolated_services: ApiServices,
) -> None:
    """A customer who fixes their card must come out of arrears.

    Deliveries are retried for days and overtake each other, so the account's
    standing cannot be whatever the last one said — it has to be derived from
    the months that are still owed, or a stale failure would keep barring an
    account that has already paid.
    """

    provider, period = _invoiced_period(isolated_services)

    # The charge at close already found this account behind.
    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(period.user_id)
    assert account is not None
    assert account.status is BillingAccountStatus.PastDue
    with isolated_services.context.database.session() as session:
        failed = BillingPeriodRepository(session).get(
            user_id=period.user_id, period_start=period.period_start
        )
    assert failed is not None
    assert failed.status is BillingPeriodStatus.PaymentFailed

    # And the payment that follows clears both, in that order or any other.
    provider.status = "paid"
    with isolated_services.context.database.session() as session:
        assert BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_paid_after_failure",
                type="invoice.paid",
                object_id=period.provider_invoice_id,
            )
        )
        session.commit()
    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(period.user_id)
        settled = BillingPeriodRepository(session).get(
            user_id=period.user_id, period_start=period.period_start
        )
    assert account is not None
    assert account.status is BillingAccountStatus.Active
    assert settled is not None
    assert settled.status is BillingPeriodStatus.Paid


def test_a_saved_card_becomes_the_one_invoices_are_charged_to(
    isolated_services: ApiServices,
) -> None:
    """Saving a card does not make it the default, and nothing does it implicitly.

    A customer who completed the hosted page and had this step skipped is
    indistinguishable from one who never saved a card at all — until the month
    closes and the charge is refused for want of a default. There is no other
    moment this platform would know to look: the customer leaves for the
    provider's page and may never return to the one that sent them.
    """

    provider = _Provider()
    provider.payment_method_owners = {"pm_saved": "cus_webhook", "pm_theirs": "cus_someone_else"}
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            plan=BillingPlan.Team,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_webhook",
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        acted = BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_setup_1",
                type="setup_intent.succeeded",
                object_id="seti_1",
                customer_id="cus_webhook",
                payment_method_id="pm_saved",
            )
        )
        session.commit()

    assert acted
    assert provider.default_payment_methods == {"cus_webhook": "pm_saved"}

    # A card saved by somebody else's integration on the same provider account
    # names a customer no account here claims, and is left alone.
    with isolated_services.context.database.session() as session:
        assert not BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_setup_2",
                type="setup_intent.succeeded",
                object_id="seti_2",
                customer_id="cus_someone_else",
                payment_method_id="pm_theirs",
            )
        )
        session.commit()
    assert provider.default_payment_methods == {"cus_webhook": "pm_saved"}

    # A delivery about a card the customer has since replaced arrives after the
    # replacement, because deliveries are retried for days. Acting on it would
    # put the old card back and charge next month to it.
    provider.payment_method_owners["pm_replaced"] = ""
    with isolated_services.context.database.session() as session:
        assert not BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_setup_stale",
                type="payment_method.attached",
                object_id="pm_replaced",
                customer_id="cus_webhook",
                payment_method_id="pm_replaced",
            )
        )
        session.commit()
    assert provider.default_payment_methods == {"cus_webhook": "pm_saved"}


def test_an_invoice_nobody_has_tried_to_charge_is_not_arrears() -> None:
    """Every invoice is unpaid the moment it is issued.

    Reading that as failure would put every account into arrears at the instant
    it was billed, and whatever reads standing to decide what may run would
    refuse them their own compute for money that was not yet due.
    """

    issued = ProviderInvoice(
        provider_invoice_id="in_fresh", total_cents=500, status="open", attempted=False
    )
    assert payment_outcome(issued) is None

    tried = ProviderInvoice(
        provider_invoice_id="in_tried", total_cents=500, status="open", attempted=True
    )
    assert payment_outcome(tried) is BillingPeriodStatus.PaymentFailed


def _invoiced_period(services: ApiServices):
    """Drive a real month all the way to an issued invoice.

    Built through the production path rather than assembled, because what the
    webhook resolves is the row that path writes: an invoice id nothing set the
    same way would prove the lookup against a fixture instead of against the
    close.
    """

    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(services.context, workspace_id)
    with services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            plan=BillingPlan.Team,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_webhook",
        )
        session.commit()

    metadata: dict[str, JsonValue] = {
        "worker_id": "worker-webhook",
        "window_start_ms": 0,
        "window_end_ms": 1000,
    }
    services.usage.append(
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-webhook",
            metric=UsageMetric.CpuSeconds,
            quantity=2_000.0,
            unit=UsageUnit.Seconds,
            labels={"cpu_millicores": "1000", "worker_id": "worker-webhook"},
            metadata=metadata,
            created_at=_USAGE_DAY,
        )
    )
    provider = _Provider()
    BillingDailyJob(services.context, services.usage).run(now=_CLOSE_RUN)
    BillingCloseJob(services.context, lambda: provider).run(now=_CLOSE_RUN)

    month = closable_month(_CLOSE_RUN)
    assert month is not None
    with services.context.database.session() as session:
        period = BillingPeriodRepository(session).get(user_id=user_id, period_start=month[0])
    assert period is not None
    # The card on file declined, which is the state a webhook has something left
    # to say about: a month correctly invoiced, correctly unpaid, and an account
    # the charge already put behind.
    assert period.status is BillingPeriodStatus.PaymentFailed
    assert period.provider_invoice_id
    return provider, period
