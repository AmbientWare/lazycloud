from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from billing.invoices import invoice_lines, to_cents
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_periods import BillingPeriodRepository
from shared.billing import BillableMetric
from shared.billing_accounts import BillingAccountStatus, BillingPlan
from shared.billing_ledger import BillingLedgerEntry
from shared.billing_periods import BillingPeriod, BillingPeriodStatus
from shared.billing_plans import DEFAULT_BILLING_PLANS
from shared.errors import ConflictError
from shared.payments import (
    HostedPaymentSession,
    InvoiceLine,
    PaymentCustomer,
    ProviderInvoice,
)
from shared.timestamps import utc_now
from shared.usage import UsageUnit
from sqlalchemy.orm import Session
from tests.service_fixtures import workspace_owner_user_id

from billing import BillingInvoiceService, BillingPeriodService, month_bounds


@dataclass(slots=True)
class _RecordingProvider:
    """Stands in for the provider, totalling its draft the way one does."""

    drafts: dict[str, list[InvoiceLine]] = field(default_factory=dict)
    finalized: list[str] = field(default_factory=list)
    period_keys: dict[str, str] = field(default_factory=dict)
    status: str = "open"
    attempted: bool = False
    card_pays: bool = True
    """Whether the card on file clears. True by default: a customer who has saved
    one is the ordinary case, and the decline is the case a test asks for."""
    default_payment_methods: dict[str, str] = field(default_factory=dict)
    payment_method_owners: dict[str, str] = field(default_factory=dict)
    """Which customer each saved card currently belongs to, as the provider would
    answer it — the read-back that stops a retried notification about a replaced
    card from putting the old one back."""

    def create_customer(self, *, email: str, workspace_id: str) -> PaymentCustomer:
        raise AssertionError("invoicing must not create customers")

    def draft_invoice(self, *, provider_customer_id: str, period_key: str) -> ProviderInvoice:
        for invoice_id, key in self.period_keys.items():
            if key == period_key:
                # Reports the state it actually reached, which is the difference
                # that lets a crash between finalizing and recording be seen.
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


def _paying_account(services: ApiServices, plan: BillingPlan) -> tuple[str, str]:
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(services.context, workspace_id)
    with services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            plan=plan,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_invoiced",
        )
        session.commit()
    return workspace_id, user_id


def _ledger(services: ApiServices, *, workspace_id: str, user_id: str, cost_nanos: int) -> None:
    day = utc_now().date()
    with services.context.database.session() as session:
        BillingLedgerRepository(session).record_day(
            workspace_id=workspace_id,
            day=day,
            entries=(
                BillingLedgerEntry(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    day=day,
                    metric=BillableMetric.GpuSeconds,
                    variant="H100",
                    quantity=100.0,
                    unit=UsageUnit.Seconds,
                    price_per_unit_nanos=1,
                    cost_nanos=cost_nanos,
                    currency="USD",
                ),
            ),
        )
        session.commit()


def _issue(
    session: Session, provider: _RecordingProvider, *, user_id: str, period_start: date
) -> BillingPeriod:
    """Reserve then issue, which is what the close job does across two commits.

    Split in production so the invoice id is durable before anything is sent to
    the provider. These tests are about what the invoice says rather than when it
    becomes visible, so they take the pair together.
    """

    service = BillingInvoiceService(session, lambda: provider)
    service.reserve(user_id=user_id, period_start=period_start)
    return service.issue(user_id=user_id, period_start=period_start)


def test_an_invoice_says_what_the_period_froze(isolated_services: ApiServices) -> None:
    """The platform decides the amount, so the invoice has to agree with it.

    Stripe is the payment rail, not the pricing engine: the lines are written
    here and the provider totals them. If those lines do not add up to the figure
    the period froze, that is a defect in this code and the customer must not be
    charged while it goes unnoticed — so issuing refuses rather than proceeds.
    """

    day = utc_now().date()
    period_start, _ = month_bounds(day)
    workspace_id, user_id = _paying_account(isolated_services, BillingPlan.Team)
    # Well under the allowance: the month owes the subscription and no more.
    _ledger(
        isolated_services, workspace_id=workspace_id, user_id=user_id, cost_nanos=40_000_000_000
    )

    provider = _RecordingProvider()
    with isolated_services.context.database.session() as session:
        closed = BillingPeriodService(session).close_for_month(user_id=user_id, day=day)
        session.commit()
    with isolated_services.context.database.session() as session:
        charged = _issue(session, provider, user_id=user_id, period_start=period_start)
        session.commit()

    team = DEFAULT_BILLING_PLANS.for_plan(BillingPlan.Team)
    lines = provider.drafts[charged.provider_invoice_id]
    assert charged.status is BillingPeriodStatus.Invoiced
    assert charged.provider_invoice_id
    assert sum(line.amount_cents for line in lines) == to_cents(closed.charged_cost_nanos)
    assert to_cents(closed.charged_cost_nanos) == to_cents(team.monthly_price_nanos)
    # The allowance offsets the usage it covered, never more: an account under
    # its allowance still owes the subscription.
    assert [line.amount_cents for line in lines if line.amount_cents < 0] == [
        -to_cents(40_000_000_000)
    ]


def test_reissuing_a_period_states_its_invoice_rather_than_adding_to_it(
    isolated_services: ApiServices,
) -> None:
    """A close is retried after a crash, and must converge on one invoice.

    The draft is found by the period it belongs to, so a second run reuses it,
    and its lines are replaced rather than appended — otherwise the retry bills
    the month twice on one document.
    """

    day = utc_now().date()
    period_start, _ = month_bounds(day)
    workspace_id, user_id = _paying_account(isolated_services, BillingPlan.Team)
    _ledger(
        isolated_services, workspace_id=workspace_id, user_id=user_id, cost_nanos=40_000_000_000
    )

    provider = _RecordingProvider()
    with isolated_services.context.database.session() as session:
        BillingPeriodService(session).close_for_month(user_id=user_id, day=day)
        session.commit()
    with isolated_services.context.database.session() as session:
        first = _issue(session, provider, user_id=user_id, period_start=period_start)
        session.commit()
    with isolated_services.context.database.session() as session:
        again = _issue(session, provider, user_id=user_id, period_start=period_start)
        session.commit()

    assert first.provider_invoice_id == again.provider_invoice_id
    assert len(provider.drafts) == 1
    # Already charged: the second run reads the period and stops.
    assert provider.finalized == [first.provider_invoice_id]


def test_a_period_still_open_cannot_be_invoiced(isolated_services: ApiServices) -> None:
    """An invoice states a settled figure, and an open period has none.

    Issuing one would put a number in front of a customer that the next
    recomputation could move.
    """

    day = utc_now().date()
    period_start, _ = month_bounds(day)
    _, user_id = _paying_account(isolated_services, BillingPlan.Team)
    with isolated_services.context.database.session() as session:
        BillingPeriodService(session).open_for_month(user_id=user_id, day=day)
        session.commit()

    provider = _RecordingProvider()
    with (
        isolated_services.context.database.session() as session,
        pytest.raises(ConflictError, match="still open"),
    ):
        _issue(session, provider, user_id=user_id, period_start=period_start)

    assert provider.drafts == {}


def test_a_ledger_that_moved_after_close_refuses_to_be_invoiced(
    isolated_services: ApiServices,
) -> None:
    """A closed period is frozen; its ledger is not. When they part, stop.

    Late usage lands in a month already settled, so the lines an invoice would
    carry stop adding up to the figure that was frozen. Charging the frozen total
    hides usage the customer ran; charging the lines contradicts what they were
    told. Neither is defensible without a decision, so this refuses and names the
    gap rather than picking one quietly.
    """

    day = utc_now().date()
    period_start, _ = month_bounds(day)
    workspace_id, user_id = _paying_account(isolated_services, BillingPlan.Team)
    _ledger(
        isolated_services, workspace_id=workspace_id, user_id=user_id, cost_nanos=40_000_000_000
    )
    with isolated_services.context.database.session() as session:
        BillingPeriodService(session).close_for_month(user_id=user_id, day=day)
        session.commit()

    # The month is settled, and then more usage arrives for it.
    _ledger(
        isolated_services, workspace_id=workspace_id, user_id=user_id, cost_nanos=250_000_000_000
    )

    provider = _RecordingProvider()
    with (
        isolated_services.context.database.session() as session,
        pytest.raises(ConflictError, match="but the period owes"),
    ):
        _issue(session, provider, user_id=user_id, period_start=period_start)

    with isolated_services.context.database.session() as session:
        unchanged = BillingPeriodRepository(session).get(user_id=user_id, period_start=period_start)
    assert unchanged is not None
    assert unchanged.status is BillingPeriodStatus.Closed
    assert not unchanged.provider_invoice_id


@pytest.mark.parametrize(
    ("plan", "usage_nanos", "parts"),
    [
        (BillingPlan.Team, 40_000_000_000, (40_000_000_000,)),
        (BillingPlan.Team, 100_000_000_000, (100_000_000_000,)),
        (BillingPlan.Team, 150_000_000_000, (120_000_000_000, 30_000_000_000)),
        (BillingPlan.Team, 0, ()),
        (BillingPlan.Free, 3_000_000_000, (3_000_000_000,)),
        (BillingPlan.Free, 400_000_000, (400_000_000,)),
        (BillingPlan.Free, 25_000_000, (5_000_000,) * 5),
    ],
)
def test_invoice_lines_always_total_what_the_period_owes(
    plan: BillingPlan, usage_nanos: int, parts: tuple[int, ...]
) -> None:
    """The lines are the invoice, so they have to add up to the frozen figure.

    Rounding each line to cents and adding them is not the same as rounding the
    total: five half-cent lines round to a cent each and to nothing together, so
    a bill built that way disagrees with itself by however many lines it has —
    and `issue` would refuse a legitimate invoice rather than send it.

    The rows are the boundaries the allowance turns on: under it, exactly on it,
    over it, and no usage at all.
    """

    config = DEFAULT_BILLING_PLANS.for_plan(plan)
    charged = config.charge_for(usage_nanos)
    period = BillingPeriod(
        id=str(uuid4()),
        user_id=str(uuid4()),
        period_start=date(2026, 8, 1),
        period_end=date(2026, 9, 1),
        status=BillingPeriodStatus.Closed,
        plan=plan,
        currency="USD",
        usage_cost_nanos=usage_nanos,
        included_cost_nanos=config.included_cost_nanos,
        subscription_cost_nanos=config.monthly_price_nanos,
        charged_cost_nanos=charged,
    )
    entries = tuple(
        BillingLedgerEntry(
            workspace_id=str(uuid4()),
            user_id=period.user_id,
            day=date(2026, 8, 3),
            metric=BillableMetric.CpuSeconds,
            variant=f"v{index}",
            quantity=1.0,
            unit=UsageUnit.Seconds,
            price_per_unit_nanos=1,
            cost_nanos=nanos,
            currency="USD",
        )
        for index, nanos in enumerate(parts)
    )

    lines = invoice_lines(period=period, entries=entries)

    assert sum(line.amount_cents for line in lines) == to_cents(charged)


def test_a_crash_between_issuing_and_recording_does_not_invoice_twice(
    isolated_services: ApiServices,
) -> None:
    """An invoice that was issued and never recorded must be adopted, not repeated.

    Finalizing is irreversible and the provider will not delete what it issued,
    so a run that finalizes and dies before committing leaves a real document the
    customer holds. Looking only for drafts would miss it — it is no longer one —
    and the next run would issue a second invoice for the same month.
    """

    day = utc_now().date()
    period_start, _ = month_bounds(day)
    workspace_id, user_id = _paying_account(isolated_services, BillingPlan.Team)
    _ledger(
        isolated_services, workspace_id=workspace_id, user_id=user_id, cost_nanos=40_000_000_000
    )
    provider = _RecordingProvider()
    with isolated_services.context.database.session() as session:
        BillingPeriodService(session).close_for_month(user_id=user_id, day=day)
        session.commit()

    # Issue, then throw away the transaction that would have recorded it.
    with isolated_services.context.database.session() as session:
        _issue(session, provider, user_id=user_id, period_start=period_start)
        session.rollback()

    issued_once = list(provider.finalized)
    with isolated_services.context.database.session() as session:
        recovered = _issue(session, provider, user_id=user_id, period_start=period_start)
        session.commit()

    assert len(provider.drafts) == 1
    # The second run finalizes nothing: it adopts what the first one issued.
    assert provider.finalized == issued_once
    assert recovered.status is BillingPeriodStatus.Invoiced
    assert recovered.provider_invoice_id == issued_once[0]
