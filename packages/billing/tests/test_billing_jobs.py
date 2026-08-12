from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from api.server.services import ApiServices
from billing.jobs import closable_month
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_periods import BillingPeriodRepository
from pydantic import JsonValue
from shared.billing_accounts import BillingAccountStatus, BillingPlan
from shared.billing_periods import BillingPeriodStatus
from shared.billing_plans import DEFAULT_BILLING_PLANS
from shared.errors import UpstreamUnavailableError
from shared.payments import (
    HostedPaymentSession,
    InvoiceLine,
    PaymentCustomer,
    ProviderInvoice,
)
from shared.usage import UsageMetric, UsageRecord, UsageUnit
from tests.service_fixtures import workspace_owner_user_id

from billing import BillingCloseJob, BillingDailyJob

# Fixed rather than relative to today. A month a sweep settles has to be one the
# rate catalog covers and one whose grace has fully elapsed, and a window derived
# from the clock satisfies both only on some days of the year — which is a test
# that passes until a calendar date nobody chose.
_USAGE_DAY = datetime(2026, 9, 15, 12, tzinfo=UTC)
_PRICING_RUN = datetime(2026, 9, 18, 6, tzinfo=UTC)
_CLOSE_RUN = datetime(2026, 10, 3, 6, tzinfo=UTC)
_IDLE_MONTH_CLOSE_RUN = datetime(2026, 11, 3, 6, tzinfo=UTC)


@dataclass(slots=True)
class _RecordingProvider:
    drafts: dict[str, list[InvoiceLine]] = field(default_factory=dict)
    finalized: list[str] = field(default_factory=list)
    period_keys: dict[str, str] = field(default_factory=dict)
    status: str = "open"
    attempted: bool = False
    card_pays: bool = True
    """Whether the card on file clears. True by default: a customer who has saved
    one is the ordinary case, and the decline is the case a test asks for."""
    charge_raises: bool = False
    default_payment_methods: dict[str, str] = field(default_factory=dict)
    payment_method_owners: dict[str, str] = field(default_factory=dict)
    """Which customer each saved card currently belongs to, as the provider would
    answer it — the read-back that stops a retried notification about a replaced
    card from putting the old one back."""

    def create_customer(self, *, email: str, workspace_id: str) -> PaymentCustomer:
        raise AssertionError("the close job must not create customers")

    def draft_invoice(self, *, provider_customer_id: str, period_key: str) -> ProviderInvoice:
        for invoice_id, key in self.period_keys.items():
            if key == period_key:
                return ProviderInvoice(
                    provider_invoice_id=invoice_id,
                    total_cents=sum(line.amount_cents for line in self.drafts[invoice_id]),
                    status="open" if invoice_id in self.finalized else "draft",
                )
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
        if self.charge_raises:
            raise UpstreamUnavailableError("the payment provider did not answer")
        self.attempted = True
        if self.card_pays:
            self.status = "paid"
        return self.fetch_invoice(provider_invoice_id=provider_invoice_id)

    def fetch_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        return ProviderInvoice(
            provider_invoice_id=provider_invoice_id,
            total_cents=sum(line.amount_cents for line in self.drafts[provider_invoice_id]),
            status=self.status if provider_invoice_id in self.finalized else "draft",
            paid=self.status == "paid",
            attempted=self.attempted,
        )

    def finalize_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        self.finalized.append(provider_invoice_id)
        return ProviderInvoice(
            provider_invoice_id=provider_invoice_id,
            total_cents=sum(line.amount_cents for line in self.drafts[provider_invoice_id]),
            status="open",
        )


def test_a_month_settles_end_to_end_and_running_again_changes_nothing(
    isolated_services: ApiServices,
) -> None:
    """Usage on a real day becomes a priced ledger, a settled month, and an invoice.

    Nothing else runs these — they are the whole reason any of the billing
    machinery executes without somebody typing a command. Both jobs are swept
    every hour, so both have to converge: the second run must find the day
    already priced and the month already invoiced, and do nothing.
    """

    workspace_id, user_id = _team_account(isolated_services)
    _record_cpu_usage(isolated_services, workspace_id=workspace_id, at=_USAGE_DAY)

    daily = BillingDailyJob(isolated_services.context, isolated_services.usage)
    provider = _RecordingProvider()
    close = BillingCloseJob(isolated_services.context, lambda: provider)

    # Two days after the day it prices: the run that would have caught it first
    # never happened, and nothing else ever revisits an unpriced day.
    daily.run(now=_PRICING_RUN)
    daily.run(now=_PRICING_RUN)

    with isolated_services.context.database.session() as session:
        priced = BillingLedgerRepository(session).for_day(workspace_id, _USAGE_DAY.date())
    assert priced
    assert sum(entry.cost_nanos for entry in priced) > 0

    # The order a scheduler tick runs them in: the month has to be priced through
    # its last day before anything settles it.
    daily.run(now=_CLOSE_RUN)

    month = closable_month(_CLOSE_RUN)
    assert month is not None
    period_start, _ = month
    assert close.run(now=_CLOSE_RUN)
    assert close.run(now=_CLOSE_RUN)

    with isolated_services.context.database.session() as session:
        period = BillingPeriodRepository(session).get(user_id=user_id, period_start=period_start)

    assert period is not None
    # Issued and charged in the same sweep: the provider does not collect on its
    # own for these invoices, so a month that ended `Invoiced` would be one
    # nobody ever took the money for.
    assert period.status is BillingPeriodStatus.Paid
    assert period.provider_invoice_id
    # The second sweep found it settled, so nothing was sent a second time.
    assert len(provider.finalized) == 1


def test_a_month_is_not_settled_until_its_last_day_has_been_priced() -> None:
    """A month closed the moment it ended freezes a total missing its last day.

    Closing is one-way: the ledger catching up afterwards cannot move an invoice
    that has already been issued, so every customer would be short a day, every
    month, permanently. The grace is what makes the pricing run that covers the
    last day one that has already finished rather than one racing this.
    """

    assert closable_month(datetime(2026, 10, 1, 0, 0, tzinfo=UTC)) is None
    assert closable_month(datetime(2026, 10, 1, 23, 59, tzinfo=UTC)) is None
    assert closable_month(_CLOSE_RUN) == (datetime(2026, 9, 1).date(), datetime(2026, 10, 1).date())


def test_a_month_with_an_unpriced_day_is_refused_rather_than_billed_short(
    isolated_services: ApiServices,
) -> None:
    """Waiting is not enough on its own; the days have to have actually been priced.

    A scheduler down longer than the pricing window leaves days nothing revisits.
    The grace elapses regardless, and closing on it alone would freeze a total
    missing those days and issue an invoice that can only be voided.
    """

    workspace_id, user_id = _team_account(isolated_services)
    _record_cpu_usage(isolated_services, workspace_id=workspace_id, at=_USAGE_DAY)

    provider = _RecordingProvider()
    close = BillingCloseJob(isolated_services.context, lambda: provider)
    month = closable_month(_CLOSE_RUN)
    assert month is not None
    period_start, _ = month

    close.run(now=_CLOSE_RUN)

    with isolated_services.context.database.session() as session:
        period = BillingPeriodRepository(session).get(user_id=user_id, period_start=period_start)
    assert period is None
    assert not provider.finalized


def test_a_subscriber_that_ran_nothing_is_still_billed(
    isolated_services: ApiServices,
) -> None:
    """A subscription is owed whether or not anything ran.

    The sweep finds payers by looking for usage, and an idle paid account has
    none. Missed here the month simply passes — the close only ever looks at the
    month that just ended, so nothing revisits it and the fee is never collected.
    """

    _, user_id = _team_account(isolated_services)

    provider = _RecordingProvider()
    BillingDailyJob(isolated_services.context, isolated_services.usage).run(
        now=_IDLE_MONTH_CLOSE_RUN
    )
    close = BillingCloseJob(isolated_services.context, lambda: provider)
    month = closable_month(_IDLE_MONTH_CLOSE_RUN)
    assert month is not None
    period_start, _ = month
    assert close.run(now=_IDLE_MONTH_CLOSE_RUN)

    with isolated_services.context.database.session() as session:
        period = BillingPeriodRepository(session).get(user_id=user_id, period_start=period_start)

    assert period is not None
    assert period.usage_cost_nanos == 0
    assert (
        period.charged_cost_nanos
        == DEFAULT_BILLING_PLANS.for_plan(BillingPlan.Team).monthly_price_nanos
    )
    assert period.status is BillingPeriodStatus.Paid
    assert len(provider.finalized) == 1


def test_a_free_account_inside_its_allowance_settles_without_a_payment_relationship(
    isolated_services: ApiServices,
) -> None:
    """Real usage, nothing owed, nobody to bill — and the month has to finish anyway.

    A free account's usage lines are real and its allowance line cancels them, so
    every line is non-zero and the total is not. Asking the payment provider for
    a customer at that point fails, because a free account has never given one:
    the month then sticks half-settled and the sweep retries it forever.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    _record_cpu_usage(isolated_services, workspace_id=workspace_id, at=_USAGE_DAY)

    provider = _RecordingProvider()
    daily = BillingDailyJob(isolated_services.context, isolated_services.usage)
    close = BillingCloseJob(isolated_services.context, lambda: provider)
    daily.run(now=_CLOSE_RUN)
    month = closable_month(_CLOSE_RUN)
    assert month is not None
    period_start, _ = month
    assert close.run(now=_CLOSE_RUN)

    with isolated_services.context.database.session() as session:
        period = BillingPeriodRepository(session).get(user_id=user_id, period_start=period_start)

    assert period is not None
    assert period.usage_cost_nanos > 0
    assert period.charged_cost_nanos == 0
    # Terminal, so the next sweep has nothing left to reconsider for this account.
    assert period.status is BillingPeriodStatus.NothingOwed
    assert not provider.finalized


def test_a_charge_that_never_completed_is_attempted_again(
    isolated_services: ApiServices,
) -> None:
    """An issued month is one still owed, however the charge ended.

    The provider does not collect on its own for these invoices, so if the charge
    fails to complete — a timeout, a 500, the process dying between two commits —
    nothing else will ever ask for the money. Treating an issued month as
    finished makes that customer's usage free, permanently, with nothing
    reporting it.
    """

    workspace_id, user_id = _team_account(isolated_services)
    _record_cpu_usage(isolated_services, workspace_id=workspace_id, at=_USAGE_DAY)

    provider = _RecordingProvider()
    provider.charge_raises = True
    daily = BillingDailyJob(isolated_services.context, isolated_services.usage)
    close = BillingCloseJob(isolated_services.context, lambda: provider)
    daily.run(now=_CLOSE_RUN)
    close.run(now=_CLOSE_RUN)

    month = closable_month(_CLOSE_RUN)
    assert month is not None
    period_start, _ = month
    with isolated_services.context.database.session() as session:
        stranded = BillingPeriodRepository(session).get(user_id=user_id, period_start=period_start)
    assert stranded is not None
    assert stranded.status is BillingPeriodStatus.Invoiced

    # The provider recovers, and the next sweep asks for the money it never got.
    provider.charge_raises = False
    close.run(now=_CLOSE_RUN)

    with isolated_services.context.database.session() as session:
        collected = BillingPeriodRepository(session).get(user_id=user_id, period_start=period_start)
    assert collected is not None
    assert collected.status is BillingPeriodStatus.Paid
    # One invoice throughout: retrying the charge must not issue a second.
    assert len(provider.finalized) == 1


def _team_account(services: ApiServices) -> tuple[str, str]:
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(services.context, workspace_id)
    with services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            plan=BillingPlan.Team,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_swept",
        )
        session.commit()
    return workspace_id, user_id


def _record_cpu_usage(services: ApiServices, *, workspace_id: str, at: datetime) -> None:
    metadata: dict[str, JsonValue] = {
        "worker_id": "worker-swept",
        "window_start_ms": 0,
        "window_end_ms": 1000,
    }
    services.usage.append(
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-swept",
            metric=UsageMetric.CpuSeconds,
            quantity=2_000.0,
            unit=UsageUnit.Seconds,
            labels={"cpu_millicores": "1000", "worker_id": "worker-swept"},
            metadata=metadata,
            created_at=at,
        )
    )
