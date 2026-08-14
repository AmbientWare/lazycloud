from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from api.server.services import ApiServices
from billing.plan_changes import CLAIM_TTL, PLAN_CHANGE_ABANDONED_ACTION
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.tables.billing_plan_changes import BillingPlanChangeIntentTable
from shared.billing_accounts import BillingAccount
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import FREE_PLAN_INCLUDED_NANOS, TEAM_PLAN_INCLUDED_NANOS
from shared.errors import UpstreamUnavailableError
from shared.events import EventLevel
from shared.payments import (
    HostedPaymentSession,
    PaymentCustomer,
    ProviderCreditGrant,
    ProviderInvoice,
    ProviderSubscription,
)
from shared.timestamps import utc_now
from sqlalchemy import select
from tests.service_fixtures import unbilled_account

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

    grants: list[int] = field(default_factory=list)
    expired_grants: list[str] = field(default_factory=list)
    plan_changes: list[BillingPlanId] = field(default_factory=list)
    subscription_reads: int = 0
    plan: BillingPlanId = BillingPlanId.Free
    status: str = "active"
    swap_error: Exception | None = None
    read_error: Exception | None = None
    grant_error: Exception | None = None

    @property
    def live_grants(self) -> list[str]:
        return [
            f"credgr_{index}"
            for index in range(1, len(self.grants) + 1)
            if f"credgr_{index}" not in self.expired_grants
        ]

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
        self, *, provider_subscription_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        del provider_subscription_id
        if self.swap_error is not None:
            raise self.swap_error
        if plan is not self.plan:
            self.plan_changes.append(plan)
            self.plan = plan
        return self._subscription()

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        del provider_subscription_id
        self.subscription_reads += 1
        if self.read_error is not None:
            raise self.read_error
        return self._subscription()

    def create_credit_grant(
        self,
        *,
        account_id: str,
        provider_customer_id: str,
        amount_nanos: int,
        period_ended_at: datetime,
        previous_period_ended_at: datetime | None,
    ) -> ProviderCreditGrant:
        del account_id, provider_customer_id, previous_period_ended_at
        if self.grant_error is not None:
            raise self.grant_error
        self.grants.append(amount_nanos)
        return ProviderCreditGrant(
            provider_credit_grant_id=f"credgr_{len(self.grants)}",
            amount_nanos=amount_nanos,
            expires_at=period_ended_at,
        )

    def expire_credit_grant(self, *, provider_credit_grant_id: str) -> None:
        # Idempotent, like the adapter: it reads the grant first and returns
        # where it has already ended, so a retry of the change that expired it
        # converges rather than recording a second expiry.
        if provider_credit_grant_id not in self.expired_grants:
            self.expired_grants.append(provider_credit_grant_id)

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        raise AssertionError("settling a plan change must not read invoices")

    def invoices_for(
        self, *, provider_customer_id: str, since: datetime, limit: int = 12
    ) -> Sequence[ProviderInvoice]:
        raise AssertionError("settling a plan change must not list invoices")

    def _subscription(self) -> ProviderSubscription:
        return ProviderSubscription(
            provider_subscription_id="sub_plan_change",
            status=self.status,
            current_period_started_at=CYCLE_STARTED_AT,
            current_period_ended_at=CYCLE_ENDED_AT,
            plan=self.plan,
        )


def test_a_plan_change_the_provider_took_is_finished_by_the_sweep(
    isolated_services: ApiServices,
) -> None:
    """The customer paid, the transaction died, and the sweep hands them the plan.

    `set_subscription_plan` raises and collects the proration inside the call,
    so anything failing after it leaves somebody charged for Team with the
    account row still naming Free — judged by admission on the free allowance
    and shown the subscribe button again. Nothing recovers that without a record
    that the change was attempted, and no delivery is guaranteed to arrive.

    Here the failure lands between the outgoing grant being expired and the
    replacement being bought, which is the worst version of it: the customer has
    paid and holds no allowance at all.
    """

    provider, user_id = _provisioned_account(isolated_services)
    _spend(isolated_services, user_id, 2_000_000_000)
    service = _plan_changes(isolated_services, provider)

    provider.grant_error = UpstreamUnavailableError("the provider stopped answering")
    with pytest.raises(UpstreamUnavailableError):
        service.subscribe(user_id=user_id)

    # The state the defect leaves behind, asserted rather than assumed: charged
    # at the provider, free on the row, and an intent that says so.
    assert provider.plan_changes == [BillingPlanId.Team]
    assert _account(isolated_services, user_id).plan is BillingPlanId.Free
    assert _intent(isolated_services).status == "settling"

    provider.grant_error = None
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
    assert provider.grants == [FREE_PLAN_INCLUDED_NANOS, TEAM_PLAN_INCLUDED_NANOS]
    assert provider.expired_grants == ["credgr_1"]
    assert provider.live_grants == [account.provider_credit_grant_id] == ["credgr_2"]
    # One swap reached the provider across both attempts: the sweep settles from
    # what the subscription says and never asks for the change again. One read is
    # the whole of what settling costs, and the request path spent none of it —
    # it already held what the swap answered with.
    assert provider.plan_changes == [BillingPlanId.Team]
    assert provider.subscription_reads == 1


def test_a_plan_change_the_provider_never_took_writes_nothing(
    isolated_services: ApiServices,
) -> None:
    """Nothing was charged, so nothing is given.

    The mirror of the case above and the more expensive one to get wrong:
    recording Team for a customer whose card was never taken hands out the
    allowance that plan includes for nothing, and the account spends a hundred
    dollars this platform will never invoice.

    The refusal here is ambiguous — the swap failed and the read that would have
    settled it failed too — which is exactly when the intent has to stay open
    rather than be guessed at.
    """

    provider, user_id = _provisioned_account(isolated_services)
    service = _plan_changes(isolated_services, provider)

    provider.swap_error = UpstreamUnavailableError("the provider stopped answering")
    provider.read_error = UpstreamUnavailableError("and would not say what it holds")
    with pytest.raises(UpstreamUnavailableError):
        service.subscribe(user_id=user_id)
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

    assert (result.applied_count, result.not_applied_count, result.open_count) == (0, 1, 0)
    assert _intent(isolated_services).status == "not_applied"
    assert account.plan is BillingPlanId.Free
    assert period is not None
    assert period.allowance_nanos == FREE_PLAN_INCLUDED_NANOS
    assert provider.grants == [FREE_PLAN_INCLUDED_NANOS]
    assert provider.expired_grants == []


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
    provider.grant_error = UpstreamUnavailableError("the provider stopped answering")
    with pytest.raises(UpstreamUnavailableError):
        service.subscribe(user_id=user_id)

    # The subscription the proration was collected on is cancelled before
    # anything settles the change, and no delivery has arrived to say so.
    before = _account(isolated_services, user_id)
    provider.status = "canceled"
    provider.grant_error = None
    result = service.settle_open(now=utc_now() + CLAIM_TTL + timedelta(seconds=1))

    assert (result.applied_count, result.abandoned_count, result.open_count) == (0, 1, 0)
    # Every column, so "nothing was written" is the claim rather than "the plan
    # was not written".
    assert _account(isolated_services, user_id) == before
    # And the allowance the plan includes was not handed out against a cycle
    # nothing will invoice.
    assert provider.grants == [FREE_PLAN_INCLUDED_NANOS]
    assert _intent(isolated_services).status == "abandoned"
    reported = isolated_services.events.list(
        workspace_id=None,
        include_cluster=True,
        actions=[PLAN_CHANGE_ABANDONED_ACTION],
    )
    assert [event.level for event in reported] == [EventLevel.Error]


def _plan_changes(services: ApiServices, provider: _Provider) -> BillingPlanChangeService:
    return BillingPlanChangeService(
        database=services.context.database,
        payments=lambda: provider,
        events=services.events,
    )


def _provisioned_account(services: ApiServices) -> tuple[_Provider, str]:
    """An account on the free plan, provisioned the way signing in provisions one."""

    user_id, workspace_id = unbilled_account(services.context)
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
