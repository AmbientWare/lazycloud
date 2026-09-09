from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from api.server.services import ApiServices
from billing.plan_changes import CLAIM_TTL, PLAN_CHANGE_ABANDONED_ACTION
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.tables.billing_plan_changes import BillingPlanChangeIntentTable
from shared.billing_accounts import BillingAccount
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import (
    FREE_PLAN_INCLUDED_NANOS,
    TEAM_PLAN_INCLUDED_NANOS,
    published_plan,
    subscription_terms,
)
from shared.errors import PaymentRequiredError, UpstreamUnavailableError
from shared.events import EventLevel
from shared.payments import (
    HostedPaymentSession,
    PaymentCustomer,
    ProviderInvoice,
    ProviderPaidSubscriptionPeriod,
    ProviderSubscription,
    SubscriptionChangeTiming,
)
from shared.timestamps import utc_now
from sqlalchemy import select
from tests.domain_fixtures import carded_account, unbilled_account

from billing import BillingAccountService, BillingPlanChangeService

CYCLE_STARTED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
CYCLE_ENDED_AT = datetime(2026, 9, 13, 9, 30, tzinfo=UTC)


@dataclass(slots=True)
class _Provider:
    """A provider whose plan, and whose willingness to answer, the test moves.

    The two are separate on purpose: what makes the window this exists to close
    dangerous is exactly that a call which failed says nothing about whether the
    change happened.
    """

    plan_changes: list[BillingPlanId] = field(default_factory=list)
    prorations: list[SubscriptionChangeTiming] = field(default_factory=list)
    subscription_reads: int = 0
    plan: BillingPlanId = BillingPlanId.Free
    scheduled_terms_version: SubscriptionTermsVersion | None = None
    status: str = "active"
    swap_error: Exception | None = None
    read_error: Exception | None = None
    credit_evidence_error: Exception | None = None

    cards_on_file: set[str] = field(default_factory=set)
    """Customers the provider says hold something chargeable."""

    def create_customer(self, *, account_id: str, email: str, workspace_id: str) -> PaymentCustomer:
        del account_id, email, workspace_id
        return PaymentCustomer(provider_customer_id="cus_plan_change")

    def card_setup_session(
        self, *, provider_customer_id: str, currency: str, success_url: str, cancel_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("settling a plan change must not open a card page")

    def customer_portal_session(
        self, *, provider_customer_id: str, return_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("settling a plan change must not open a portal page")

    def payment_method_owner(self, *, provider_payment_method_id: str) -> str:
        raise AssertionError("settling a plan change must not read cards")

    def has_payment_method(self, *, provider_customer_id: str) -> bool:
        return provider_customer_id in self.cards_on_file

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        raise AssertionError("settling a plan change must not change the default card")

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
        raise AssertionError("settling a plan change must not meter usage")

    def create_subscription(
        self, *, provider_customer_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        del provider_customer_id
        self.plan = plan
        return self._subscription()

    def set_subscription_plan(
        self,
        *,
        provider_subscription_id: str,
        terms_version: SubscriptionTermsVersion,
        timing: SubscriptionChangeTiming,
        operation_id: str,
        operation_created_at: datetime,
    ) -> ProviderSubscription:
        plan = subscription_terms(terms_version).plan
        del provider_subscription_id
        if self.swap_error is not None:
            raise self.swap_error
        if plan is self.plan:
            self.scheduled_terms_version = None
        elif timing is SubscriptionChangeTiming.AtRenewal:
            self.scheduled_terms_version = terms_version
        else:
            self.plan_changes.append(plan)
            self.prorations.append(timing)
            self.plan = plan
            self.scheduled_terms_version = None
        return self._subscription()

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        del provider_subscription_id
        self.subscription_reads += 1
        if self.read_error is not None:
            raise self.read_error
        return self._subscription()

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        raise AssertionError("settling a plan change must not read invoices")

    def paid_subscription_periods(
        self, *, provider_customer_id: str, provider_subscription_id: str, since: datetime
    ) -> Sequence[ProviderPaidSubscriptionPeriod]:
        if self.credit_evidence_error is not None:
            raise self.credit_evidence_error
        return (
            ProviderPaidSubscriptionPeriod(
                provider_invoice_id=f"in_{self.plan.value}",
                provider_invoice_line_id=f"il_{self.plan.value}",
                provider_subscription_id=provider_subscription_id,
                plan=self.plan,
                period_started_at=CYCLE_STARTED_AT,
                period_ended_at=CYCLE_ENDED_AT,
                prorated=False,
                amount_nanos=100_000_000_000,
                invoice_paid_nanos=100_000_000_000,
                paid_at=utc_now(),
                terms_version=published_plan(self.plan).terms_version,
            ),
        )

    def invoices_for(
        self, *, provider_customer_id: str, since: datetime, limit: int | None = 12
    ) -> Sequence[ProviderInvoice]:
        raise AssertionError("settling a plan change must not list invoices")

    def _subscription(self) -> ProviderSubscription:
        return ProviderSubscription(
            provider_subscription_id="sub_plan_change",
            status=self.status,
            current_period_started_at=CYCLE_STARTED_AT,
            current_period_ended_at=CYCLE_ENDED_AT,
            plan=self.plan,
            terms_version=published_plan(self.plan).terms_version
            if self.plan is not None
            else None,
            scheduled_terms_version=self.scheduled_terms_version,
            scheduled_change_at=CYCLE_ENDED_AT if self.scheduled_terms_version else None,
        )


def test_a_plan_change_the_provider_took_is_finished_by_the_sweep(
    isolated_services: ApiServices,
) -> None:
    """Recover a paid change after its local funding transaction failed."""

    provider, user_id = _provisioned_account(isolated_services)
    _spend(isolated_services, user_id, 2_000_000_000)
    service = _plan_changes(isolated_services, provider)

    provider.credit_evidence_error = UpstreamUnavailableError("the provider stopped answering")
    with pytest.raises(UpstreamUnavailableError):
        service.change_plan(
            user_id=user_id,
            target=BillingPlanId.Team,
            target_terms_version=published_plan(BillingPlanId.Team).terms_version,
        )

    # The state the defect leaves behind, asserted rather than assumed: charged
    # at the provider, free on the row, and an intent that says so.
    assert provider.plan_changes == [BillingPlanId.Team]
    assert _account(isolated_services, user_id).plan is BillingPlanId.Free
    assert _intent(isolated_services).status == "settling"

    provider.credit_evidence_error = None
    result = service.settle_open(now=utc_now() + CLAIM_TTL + timedelta(seconds=1))

    account = _account(isolated_services, user_id)
    with isolated_services.context.database.session() as session:
        period = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )

    assert (result.applied_count, result.not_applied_count, result.open_count) == (1, 0, 0)
    assert account.plan is BillingPlanId.Team
    assert _intent(isolated_services).status == "applied"
    assert period is not None
    assert period.allowance_nanos == TEAM_PLAN_INCLUDED_NANOS
    # What was spent while the account was free survives: the usage is on the
    # same invoice the prorated plan fee lands on.
    assert period.spent_nanos == 2_000_000_000
    with isolated_services.context.database.session() as session:
        assert (
            BillingCreditRepository(session).subscription_issued(
                user_id=user_id, period_ended_at=CYCLE_ENDED_AT
            )
            == TEAM_PLAN_INCLUDED_NANOS
        )
    assert provider.plan_changes == [BillingPlanId.Team]


def test_a_plan_change_the_provider_never_took_writes_nothing(
    isolated_services: ApiServices,
) -> None:
    """An ambiguous provider failure keeps the intent pending and grants no credit."""

    provider, user_id = _provisioned_account(isolated_services)
    service = _plan_changes(isolated_services, provider)

    provider.swap_error = UpstreamUnavailableError("the provider stopped answering")
    provider.read_error = UpstreamUnavailableError("and would not say what it holds")
    with pytest.raises(UpstreamUnavailableError):
        service.change_plan(
            user_id=user_id,
            target=BillingPlanId.Team,
            target_terms_version=published_plan(BillingPlanId.Team).terms_version,
        )
    # Paced back into the queue rather than closed on a read that answered
    # nothing: the intent is the only record that a change was attempted, and a
    # swap that lands after it was thrown away is a charge nobody can find.
    assert _intent(isolated_services).status == "pending"

    provider.read_error = None
    result = service.settle_open(now=utc_now() + CLAIM_TTL + timedelta(seconds=1))

    account = _account(isolated_services, user_id)
    with isolated_services.context.database.session() as session:
        period = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )

    assert (result.applied_count, result.retried_count, result.open_count) == (0, 1, 1)
    assert _intent(isolated_services).status == "pending"
    assert account.plan is BillingPlanId.Free
    assert period is not None
    assert period.allowance_nanos == FREE_PLAN_INCLUDED_NANOS
    with isolated_services.context.database.session() as session:
        assert (
            BillingCreditRepository(session).subscription_issued(
                user_id=user_id, period_ended_at=CYCLE_ENDED_AT
            )
            == FREE_PLAN_INCLUDED_NANOS
        )


def test_a_plan_change_whose_subscription_ended_is_never_written_back(
    isolated_services: ApiServices,
) -> None:
    """A dead subscription is not something an account can be put back onto.

    The provider keeps a cancelled subscription's items, so it still answers
    with the plan the swap put on it — and settling from the plan alone would
    write that plan and that subscription onto the row, and buy the allowance
    the plan includes against a cycle nothing will invoice. Admission asks only
    whether an account names a subscription and a plan, so the account would go
    on being admitted while every meter event it produced reached no bill: the
    exact state ending a subscription clears the row to prevent, recreated by
    the sweep that exists to protect the money.

    Nobody here can decide it, so nothing is written and an operator is told.
    """

    provider, user_id = _provisioned_account(isolated_services)
    service = _plan_changes(isolated_services, provider)
    provider.credit_evidence_error = UpstreamUnavailableError("the provider stopped answering")
    with pytest.raises(UpstreamUnavailableError):
        service.change_plan(
            user_id=user_id,
            target=BillingPlanId.Team,
            target_terms_version=published_plan(BillingPlanId.Team).terms_version,
        )

    # The subscription the proration was collected on is cancelled before
    # anything settles the change, and no delivery has arrived to say so.
    before = _account(isolated_services, user_id)
    provider.status = "canceled"
    provider.credit_evidence_error = None
    result = service.settle_open(now=utc_now() + CLAIM_TTL + timedelta(seconds=1))

    assert (result.applied_count, result.abandoned_count, result.open_count) == (0, 1, 0)
    # Every column, so "nothing was written" is the claim rather than "the plan
    # was not written".
    assert _account(isolated_services, user_id) == before
    # And the allowance the plan includes was not handed out against a cycle
    # nothing will invoice.
    with isolated_services.context.database.session() as session:
        assert (
            BillingCreditRepository(session).subscription_issued(
                user_id=user_id, period_ended_at=CYCLE_ENDED_AT
            )
            == FREE_PLAN_INCLUDED_NANOS
        )
    assert _intent(isolated_services).status == "abandoned"
    reported = isolated_services.events.list(
        workspace_id=None,
        actions=[PLAN_CHANGE_ABANDONED_ACTION],
    )
    assert [event.level for event in reported] == [EventLevel.Error]


def test_moving_to_a_cheaper_plan_keeps_the_allowance_this_cycle_opened_with(
    isolated_services: ApiServices,
) -> None:
    """A scheduled downgrade preserves paid terms and credit through renewal."""

    provider, user_id = _provisioned_account(isolated_services)
    service = _plan_changes(isolated_services, provider)

    service.change_plan(
        user_id=user_id,
        target=BillingPlanId.Team,
        target_terms_version=published_plan(BillingPlanId.Team).terms_version,
    )
    _spend(isolated_services, user_id, 40_000_000_000)
    account = service.change_plan(
        user_id=user_id,
        target=BillingPlanId.Free,
        target_terms_version=published_plan(BillingPlanId.Free).terms_version,
    )

    with isolated_services.context.database.session() as session:
        period = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )

    assert account.plan is BillingPlanId.Team
    assert account.scheduled_terms_version is SubscriptionTermsVersion.Free
    assert period is not None
    assert (period.allowance_nanos, period.spent_nanos) == (
        TEAM_PLAN_INCLUDED_NANOS,
        40_000_000_000,
    )
    with isolated_services.context.database.session() as session:
        assert (
            BillingCreditRepository(session).subscription_issued(
                user_id=user_id, period_ended_at=CYCLE_ENDED_AT
            )
            == TEAM_PLAN_INCLUDED_NANOS
        )
    assert provider.plan_changes == [BillingPlanId.Team]
    assert provider.prorations == [SubscriptionChangeTiming.Immediate]


def _plan_changes(services: ApiServices, provider: _Provider) -> BillingPlanChangeService:
    return BillingPlanChangeService(
        database=services.context.database,
        payments=lambda: provider,
        events=services.events,
    )


def _provisioned_account(services: ApiServices) -> tuple[_Provider, str]:
    """Create one Free account with a saved payment method."""

    user_id, workspace_id = carded_account(services.context)
    provider = _Provider()
    with services.context.database.session() as session:
        BillingAccountService(session).billing_account_for(
            provider, user_id=user_id, workspace_id=workspace_id
        )
    return provider, user_id


def _spend(services: ApiServices, user_id: str, cost_nanos: int) -> None:
    with services.context.database.session() as session:
        BillingAllowanceRepository(session).increment(
            user_id=user_id, at=CYCLE_STARTED_AT, cost_nanos=cost_nanos
        )


def _account(services: ApiServices, user_id: str) -> BillingAccount:
    with services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
    assert account is not None
    return account


def _intent(services: ApiServices) -> BillingPlanChangeIntentTable:
    with services.context.database.session() as session:
        return session.scalars(select(BillingPlanChangeIntentTable)).one()


def test_subscribing_without_a_card_is_refused_before_an_intent_exists(
    isolated_services: ApiServices,
) -> None:
    """Refuse an unfundable upgrade before opening its durable intent."""

    user_id, workspace_id = unbilled_account(isolated_services.context)
    provider = _Provider()
    with isolated_services.context.database.session() as session:
        BillingAccountService(session).billing_account_for(
            provider, user_id=user_id, workspace_id=workspace_id
        )
        session.commit()

    service = _plan_changes(isolated_services, provider)
    with pytest.raises(PaymentRequiredError):
        service.change_plan(
            user_id=user_id,
            target=BillingPlanId.Team,
            target_terms_version=published_plan(BillingPlanId.Team).terms_version,
        )

    with isolated_services.context.database.session() as session:
        intents = session.scalars(select(BillingPlanChangeIntentTable)).all()
        account = BillingAccountRepository(session).get_by_user(user_id)
    assert intents == [], "a refused subscribe left an intent holding the account's slot"
    assert account is not None
    assert account.plan is BillingPlanId.Free
    assert provider.plan_changes == [], "the provider was asked to swap a plan nobody can pay for"


def test_returning_to_a_plan_inside_one_cycle_is_not_charged_twice(
    isolated_services: ApiServices,
) -> None:
    """Canceling a scheduled downgrade preserves the held subscription without a charge."""

    provider, user_id = _provisioned_account(isolated_services)
    service = _plan_changes(isolated_services, provider)

    service.change_plan(
        user_id=user_id,
        target=BillingPlanId.Team,
        target_terms_version=published_plan(BillingPlanId.Team).terms_version,
    )
    service.change_plan(
        user_id=user_id,
        target=BillingPlanId.Free,
        target_terms_version=published_plan(BillingPlanId.Free).terms_version,
    )
    service.change_plan(
        user_id=user_id,
        target=BillingPlanId.Team,
        target_terms_version=published_plan(BillingPlanId.Team).terms_version,
    )

    assert _account(isolated_services, user_id).scheduled_terms_version is None
    assert provider.plan_changes == [BillingPlanId.Team]
    assert provider.prorations == [SubscriptionChangeTiming.Immediate]

    with isolated_services.context.database.session() as session:
        period = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )
    assert period is not None
    assert period.allowance_nanos == TEAM_PLAN_INCLUDED_NANOS
