from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from api.server.services import ApiServices
from billing.reconciliation import (
    RECONCILIATION_DIVERGENCE_ACTION,
    BillingDivergence,
    BillingReconciliationService,
)
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from database.tables.observability import UsageRecordTable
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.billing_quotes import BilledDimension, LedgerBasis, LedgerComponent
from shared.billing_rate_card import published_plan
from shared.events import EventLevel
from shared.payments import (
    HostedPaymentSession,
    PaymentCustomer,
    ProviderCreditGrant,
    ProviderInvoice,
    ProviderSubscription,
    SubscriptionProration,
)
from tests.service_fixtures import unbilled_account, workspace_owner_user_id

CYCLE_STARTED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
CYCLE_ENDED_AT = datetime(2026, 9, 13, 9, 30, tzinfo=UTC)
PREVIOUS_STARTED_AT = datetime(2026, 7, 13, 9, 30, tzinfo=UTC)
PREVIOUS_ENDED_AT = CYCLE_STARTED_AT


@dataclass(slots=True)
class _Provider:
    """A provider whose plan, invoices and metered totals the test states."""

    plan: BillingPlanId = BillingPlanId.Team
    billed_customer_id: str = ""
    """Whose invoices `invoices` are. Every other customer has none, which is
    what keeps a test about one account's usage from also describing every other
    account this installation holds."""

    invoices: tuple[ProviderInvoice, ...] = ()
    metered: Mapping[str, int] = field(default_factory=lambda: dict[str, int]())
    invoice_reads: list[str] = field(default_factory=list)

    cards_on_file: set[str] = field(default_factory=set)
    """Customers the provider says hold something chargeable."""

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        return ProviderSubscription(
            provider_subscription_id=provider_subscription_id,
            status="active",
            current_period_started_at=CYCLE_STARTED_AT,
            current_period_ended_at=CYCLE_ENDED_AT,
            plan=self.plan,
        )

    def invoices_for(
        self, *, provider_customer_id: str, since: datetime, limit: int = 12
    ) -> Sequence[ProviderInvoice]:
        del since, limit
        return self.invoices if provider_customer_id == self.billed_customer_id else ()

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        self.invoice_reads.append(provider_invoice_id)
        return self.metered

    def create_customer(self, *, account_id: str, email: str, workspace_id: str) -> PaymentCustomer:
        raise AssertionError("reconciling must not register anyone")

    def card_setup_session(
        self, *, provider_customer_id: str, currency: str, success_url: str, cancel_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("reconciling must not open a card page")

    def customer_portal_session(
        self, *, provider_customer_id: str, return_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("reconciling must not open a portal page")

    def payment_method_owner(self, *, provider_payment_method_id: str) -> str:
        raise AssertionError("reconciling must not read cards")

    def has_payment_method(self, *, provider_customer_id: str) -> bool:
        return provider_customer_id in self.cards_on_file

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        raise AssertionError("reconciling must not change cards")

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
        raise AssertionError("reconciling must not meter usage")

    def create_subscription(
        self, *, provider_customer_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        raise AssertionError("reconciling must not subscribe anyone")

    def set_subscription_plan(
        self,
        *,
        provider_subscription_id: str,
        plan: BillingPlanId,
        proration: SubscriptionProration,
    ) -> ProviderSubscription:
        raise AssertionError("reconciling must not change anyone's plan")

    def create_credit_grant(
        self,
        *,
        account_id: str,
        provider_customer_id: str,
        amount_nanos: int,
        period_ended_at: datetime,
        previous_period_ended_at: datetime | None,
    ) -> ProviderCreditGrant:
        raise AssertionError("reconciling must not grant an allowance")

    def expire_credit_grant(self, *, provider_credit_grant_id: str) -> None:
        raise AssertionError("reconciling must not expire an allowance")


def test_a_plan_changed_at_the_provider_is_reported_and_never_corrected(
    isolated_services: ApiServices,
) -> None:
    """A disagreement about money is reported once and acted on by nobody.

    The pass observes objects nothing here recorded an intent about, so it
    cannot know whether a difference is a delivery that never arrived or a
    deliberate change somebody made in the provider's dashboard. Writing either
    side onto the other would be a money write on a guess — an account handed a
    plan's allowance because a page was clicked somewhere, or a paid plan
    quietly reverted. So both halves are asserted: the disagreement is recorded,
    and the account is exactly as it was.

    Reported once, too. An hourly pass that emitted a fresh error event for the
    same unchanged disagreement would bury the report it exists to make.
    """

    user_id, _ = unbilled_account(isolated_services.context)
    _account_row(isolated_services, user_id, BillingPlanId.Free)
    # The other account this installation holds agrees with the provider, and is
    # here so that "one of them was reported" is a claim about the divergent one
    # rather than about everything the pass walked.
    with isolated_services.context.database.session() as session:
        default_workspace_id = isolated_services.context.default_workspace_id(session)
    agreeing_user_id = workspace_owner_user_id(isolated_services.context, default_workspace_id)
    _account_row(isolated_services, agreeing_user_id, BillingPlanId.Team)
    provider = _Provider()
    service = BillingReconciliationService(
        database=isolated_services.context.database,
        payments=lambda: provider,
        events=isolated_services.events,
    )

    before = _account_state(isolated_services, user_id)
    first = service.reconcile(now=CYCLE_STARTED_AT + timedelta(days=1))
    after = _account_state(isolated_services, user_id)

    assert (first.accounts_checked, first.divergent_count, first.unreachable_count) == (2, 1, 0)
    assert after == before
    reported = isolated_services.events.list(
        workspace_id=None,
        actions=[RECONCILIATION_DIVERGENCE_ACTION],
    )
    assert [(event.level, event.resource_id) for event in reported] == [(EventLevel.Error, user_id)]
    assert reported[0].data["divergences"] == [BillingDivergence.PlanDisagrees.value]

    second = service.reconcile(now=CYCLE_STARTED_AT + timedelta(days=1))

    assert second.divergent_count == 1
    assert _account_state(isolated_services, user_id) == before
    assert (
        len(
            isolated_services.events.list(
                workspace_id=None,
                actions=[RECONCILIATION_DIVERGENCE_ACTION],
            )
        )
        == 1
    )


def test_usage_nobody_will_ever_be_charged_for_is_reported_rather_than_balanced(
    isolated_services: ApiServices,
) -> None:
    """A charge the outbox gave up on is named, and a proration is not compared.

    Two ways this comparison can say nothing while money is missing, and both
    are silence rather than a wrong number.

    A meter event that was abandoned never reaches the invoice, so subtracting
    it is what keeps an account that lost one from reading as a disagreement
    about a figure that is not in dispute — and subtracting it without saying so
    is the ledger agreeing with the bill about usage nobody was billed for. It
    is the one difference here that never resolves itself.

    The invoice compared has to cover a span. A plan change is prorated onto an
    invoice raised there and then, which carries no metered line and covers no
    time; taken as the newest closed period it would compare nothing against
    nothing, agree, and leave the account with no usage reconciliation from its
    upgrade onwards.
    """

    user_id, workspace_id = unbilled_account(isolated_services.context)
    _account_row(isolated_services, user_id, BillingPlanId.Free)
    _priced_usage(isolated_services, workspace_id, user_id, costs=(10_000, 4_000))
    _abandoned_delivery(isolated_services, workspace_id, user_id, value_nanos=4_000)
    provider = _Provider(
        plan=BillingPlanId.Free,
        billed_customer_id=f"cus_{user_id}",
        invoices=(
            ProviderInvoice(
                provider_invoice_id="in_closed",
                status="paid",
                period_started_at=PREVIOUS_STARTED_AT,
                period_ended_at=PREVIOUS_ENDED_AT,
            ),
            # Raised the instant a plan changed, and the newest thing here.
            ProviderInvoice(
                provider_invoice_id="in_proration",
                status="paid",
                period_started_at=CYCLE_STARTED_AT + timedelta(hours=12),
                period_ended_at=CYCLE_STARTED_AT + timedelta(hours=12),
            ),
        ),
        metered={"lazycloud_compute_cost_nanos": 10_000},
    )

    BillingReconciliationService(
        database=isolated_services.context.database,
        payments=lambda: provider,
        events=isolated_services.events,
    ).reconcile(now=CYCLE_STARTED_AT + timedelta(days=1))

    # The proration invoice is the newest of the two and was never compared.
    assert provider.invoice_reads == ["in_closed"]
    reported = [
        event
        for event in isolated_services.events.list(
            workspace_id=None,
                actions=[RECONCILIATION_DIVERGENCE_ACTION],
        )
        if event.resource_id == user_id
    ]
    # The abandoned charge and nothing else: the arithmetic balances once it is
    # taken off both sides, which is what keeps it from also arriving as a
    # figure that does not add up.
    assert [(event.level, event.data["divergences"]) for event in reported] == [
        (EventLevel.Error, [BillingDivergence.UsageAbandoned.value])
    ]


def _priced_usage(
    services: ApiServices, workspace_id: str, user_id: str, *, costs: tuple[int, ...]
) -> None:
    """Ledger segments this payer's last closed cycle was billed for."""

    with services.context.database.session() as session:
        for index, cost_nanos in enumerate(costs):
            usage_record_id = str(uuid4())
            session.add(
                UsageRecordTable(
                    id=usage_record_id,
                    workspace_id=workspace_id,
                    resource_type="container",
                    resource_id=str(uuid4()),
                    metric="container_runtime_seconds",
                    quantity=60.0,
                    payload={},
                )
            )
            session.flush()
            session.add(
                BillingLedgerSegmentTable(
                    id=str(uuid4()),
                    usage_record_id=usage_record_id,
                    segment_index=0,
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    dimension=BilledDimension.ComputeRuntime.value,
                    component=LedgerComponent.ContainerTime.value,
                    basis=LedgerBasis.Reserved.value,
                    subject_type="container",
                    subject_id=str(uuid4()),
                    span_started_at=PREVIOUS_STARTED_AT + timedelta(hours=index),
                    span_ended_at=PREVIOUS_STARTED_AT + timedelta(hours=index, minutes=1),
                    segment_started_at=PREVIOUS_STARTED_AT + timedelta(hours=index),
                    segment_ended_at=PREVIOUS_STARTED_AT + timedelta(hours=index, minutes=1),
                    duration_ms=60_000,
                    quantity=Decimal("60"),
                    pricing_version="2026-08-13.a",
                    rate_nanos_per_unit=Decimal("1000"),
                    quote_effective_at=PREVIOUS_STARTED_AT,
                    cost_nanos=cost_nanos,
                )
            )


def _abandoned_delivery(
    services: ApiServices, workspace_id: str, user_id: str, *, value_nanos: int
) -> None:
    """A meter event inside that cycle the provider will never be offered again."""

    with services.context.database.session() as session:
        session.add(
            BillingMeterOutboxTable(
                id=str(uuid4()),
                workspace_id=workspace_id,
                identifier=str(uuid4()),
                provider_customer_id=f"cus_{user_id}",
                meter_event_name="lazycloud_compute_cost_nanos",
                value_nanos=value_nanos,
                pricing_version="2026-08-13.a",
                occurred_at=PREVIOUS_STARTED_AT + timedelta(hours=1),
                status="abandoned",
                attempts=12,
                next_attempt_at=PREVIOUS_STARTED_AT,
            )
        )


def _account_row(services: ApiServices, user_id: str, plan: BillingPlanId) -> None:
    """A subscribed account on a plan, in the cycle the provider is also in."""

    with services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_{user_id}",
            provider_subscription_id=f"sub_{user_id}",
            provider_credit_grant_id=f"credgr_{user_id}",
            plan=plan,
        )
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=user_id,
            period_started_at=CYCLE_STARTED_AT,
            period_ended_at=CYCLE_ENDED_AT,
            allowance_nanos=published_plan(plan).included_nanos,
            funded=True,
        )


def _account_state(services: ApiServices, user_id: str) -> tuple[object, ...]:
    """Everything a correction would have moved, read as one comparable value."""

    with services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
        period = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )
    assert account is not None
    assert period is not None
    return (
        account.plan,
        account.status,
        account.provider_subscription_id,
        account.provider_credit_grant_id,
        account.updated_at,
        period.started_at,
        period.ended_at,
        period.allowance_nanos,
        period.spent_nanos,
    )
