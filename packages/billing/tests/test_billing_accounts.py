from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from billing.costs import BillingStandingService
from billing.periods import carry_plan_into_cycle
from control.service import ControlPlaneService
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.identity import (
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from database.repositories.orchestration import ContainerRepository
from database.tables.billing_credits import BillingCreditLotTable
from shared.billing_accounts import BillingAccountStatus
from shared.billing_credits import CreditKind
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import (
    FREE_PLAN_INCLUDED_NANOS,
    NO_CARD_MAX_CPU_CONTAINERS,
    ONE_TIME_TRIAL_NANOS,
    TEAM_PLAN_INCLUDED_NANOS,
    TRIAL_VALIDITY_DAYS,
    published_plan,
    subscription_terms,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import CapacityLimitReachedError, PaymentRequiredError, UpstreamUnavailableError
from shared.identity import WorkspaceRole
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
from tests.service_fixtures import (
    carded_account,
    owned_workspace,
    unbilled_account,
    workspace_owner_user_id,
)

from billing import BillingAccountService, BillingPlanChangeService, DatabaseBillingAdmission

CYCLE_STARTED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
CYCLE_ENDED_AT = datetime(2026, 9, 13, 9, 30, tzinfo=UTC)


@dataclass(slots=True)
class _Provider:
    """A payment provider that records what it was asked to create and change."""

    customers: list[str] = field(default_factory=list)
    subscriptions: list[BillingPlanId] = field(default_factory=list)
    plan_changes: list[BillingPlanId] = field(default_factory=list)

    plan: BillingPlanId = BillingPlanId.Free
    invoice_paid: bool = True
    cycle_started_at: datetime = CYCLE_STARTED_AT
    cycle_ended_at: datetime = CYCLE_ENDED_AT

    cards_on_file: set[str] = field(default_factory=set)
    """Customers the provider says hold something chargeable."""

    def create_customer(self, *, account_id: str, email: str, workspace_id: str) -> PaymentCustomer:
        del account_id, email
        self.customers.append(workspace_id)
        return PaymentCustomer(provider_customer_id="cus_subscriber")

    def card_setup_session(
        self, *, provider_customer_id: str, currency: str, success_url: str, cancel_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("subscribing must not open a card page")

    def customer_portal_session(
        self, *, provider_customer_id: str, return_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("subscribing must not open a portal page")

    def payment_method_owner(self, *, provider_payment_method_id: str) -> str:
        raise AssertionError("subscribing must not read cards")

    def has_payment_method(self, *, provider_customer_id: str) -> bool:
        return provider_customer_id in self.cards_on_file

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        raise AssertionError("subscribing must not change the default card")

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
        raise AssertionError("subscribing must not meter usage")

    def create_subscription(
        self, *, provider_customer_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        del provider_customer_id
        self.subscriptions.append(plan)
        self.plan = plan
        return ProviderSubscription(
            provider_subscription_id=f"sub_{len(self.subscriptions)}",
            status="active",
            current_period_started_at=self.cycle_started_at,
            current_period_ended_at=self.cycle_ended_at,
            plan=plan,
            terms_version=published_plan(plan).terms_version,
            scheduled_terms_version=None,
            scheduled_change_at=None,
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
        plan = subscription_terms(terms_version).plan
        if plan is not self.plan:
            self.plan_changes.append(plan)
            self.plan = plan
        return ProviderSubscription(
            provider_subscription_id=provider_subscription_id,
            status="active",
            # A plan change does not re-anchor the cycle, which is why the period
            # an upgrade re-terms is the same row — unless the cycle has rolled
            # underneath it, which is what a renewal did and this did not.
            current_period_started_at=self.cycle_started_at,
            current_period_ended_at=self.cycle_ended_at,
            plan=self.plan,
            terms_version=published_plan(self.plan).terms_version
            if self.plan is not None
            else None,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        return ProviderSubscription(
            provider_subscription_id=provider_subscription_id,
            status="active",
            current_period_started_at=self.cycle_started_at,
            current_period_ended_at=self.cycle_ended_at,
            plan=self.plan,
            terms_version=published_plan(self.plan).terms_version
            if self.plan is not None
            else None,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        raise AssertionError("subscribing must not read invoices")

    def paid_subscription_periods(
        self, *, provider_customer_id: str, provider_subscription_id: str, since: datetime
    ) -> Sequence[ProviderPaidSubscriptionPeriod]:
        if not self.invoice_paid:
            return ()
        return (
            ProviderPaidSubscriptionPeriod(
                provider_invoice_id=f"in_{self.plan.value}",
                provider_invoice_line_id=f"il_{self.plan.value}",
                provider_subscription_id=provider_subscription_id,
                plan=self.plan,
                period_started_at=self.cycle_started_at,
                period_ended_at=self.cycle_ended_at,
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
        raise AssertionError("subscribing must not list invoices")


def _plan_changes(services: ApiServices, provider: _Provider) -> BillingPlanChangeService:
    return BillingPlanChangeService(
        database=services.context.database,
        payments=lambda: provider,
        events=services.events,
    )


def test_a_workspace_is_judged_on_its_owners_account_and_nobody_elses(
    isolated_services: ApiServices,
) -> None:
    """Who pays is reached through the workspace's owner, and stops there.

    One account backs every workspace its owner holds, so the standing that
    decides admission is the owner's. Reaching it through membership instead
    would let one person's unpaid card refuse work in every workspace they have
    ever been added to — which is why the payer is given a non-owner membership
    of the stranger's workspace here, and why the stranger's workspace still
    starts work.
    """

    control = ControlPlaneService(isolated_services.context)
    stranger = owned_workspace(control, "stranger")
    with isolated_services.context.database.session() as session:
        paying_workspace_id = isolated_services.context.default_workspace_id(session)
    paying_user_id = workspace_owner_user_id(isolated_services.context, paying_workspace_id)
    with isolated_services.context.database.session() as session:
        # Provisioned, and past due on top of it. Both workspaces' owners hold a
        # subscription — admission asks every account whether its usage has
        # anywhere to be billed — so what separates the two answers here is the
        # standing, which is the whole subject.
        BillingAccountRepository(session).upsert(
            user_id=paying_user_id,
            status=BillingAccountStatus.PastDue,
            provider_customer_id="cus_paying",
            provider_subscription_id="sub_paying",
            plan=BillingPlanId.Team,
            subscription_terms_version=published_plan(BillingPlanId.Team).terms_version,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        WorkspaceMemberRepository(session).add(
            workspace_id=stranger.id,
            user_id=paying_user_id,
            role=WorkspaceRole.Member,
        )
        session.commit()

    admission = DatabaseBillingAdmission()
    with isolated_services.context.database.session() as session:
        with pytest.raises(PaymentRequiredError):
            admission.admit_container_start(
                session, workspace_id=paying_workspace_id, gpu=(), gpu_count=0
            )
        admission.admit_container_start(session, workspace_id=stranger.id, gpu=(), gpu_count=0)


def test_trial_is_once_per_account_and_free_renewal_does_not_replenish_it(
    postgres_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("billing.admission.utc_now", lambda: CYCLE_STARTED_AT)
    provider = _Provider()
    user_id, workspace_id = unbilled_account(postgres_services.context)
    with postgres_services.context.database.session() as session:
        service = BillingAccountService(session)
        first = service.billing_account_for(provider, user_id=user_id, workspace_id=workspace_id)
        again = service.billing_account_for(provider, user_id=user_id, workspace_id=workspace_id)
        assert first.provider_subscription_id == again.provider_subscription_id
        lots = list(
            session.scalars(
                select(BillingCreditLotTable).where(BillingCreditLotTable.user_id == user_id)
            )
        )
        assert len(lots) == 1
        trial = lots[0]
        original = (trial.id, trial.amount_nanos, trial.effective_at, trial.expires_at)
        assert trial.kind == CreditKind.Trial.value
        assert trial.amount_nanos == ONE_TIME_TRIAL_NANOS
        assert trial.expires_at is not None
        assert trial.expires_at == trial.effective_at + timedelta(days=TRIAL_VALIDITY_DAYS)
        credits = BillingCreditRepository(session)
        assert credits.balance(user_id=user_id, at=CYCLE_STARTED_AT) == ONE_TIME_TRIAL_NANOS
        displayed = BillingStandingService(session).credit_balance(
            user_id=user_id, at=CYCLE_STARTED_AT
        )
        assert displayed.ready and displayed.balance_nanos == ONE_TIME_TRIAL_NANOS
        DatabaseBillingAdmission().assert_may_take_on_billed_work(
            session, workspace_id=workspace_id
        )
        assert credits.balance(user_id=user_id, at=trial.expires_at) == 0

    provider.cycle_started_at = CYCLE_ENDED_AT
    provider.cycle_ended_at = CYCLE_ENDED_AT + timedelta(days=30)
    with postgres_services.context.database.session() as session:
        subscription = provider.subscription(
            provider_subscription_id=first.provider_subscription_id
        )
        for _ in range(2):
            carry_plan_into_cycle(
                session,
                provider,
                account_id=user_id,
                provider_customer_id=first.provider_customer_id,
                subscription=subscription,
                plan=BillingPlanId.Free,
            )
        accounts = BillingAccountRepository(session)
        accounts.upsert(
            user_id=user_id,
            status=first.status,
            provider_customer_id=first.provider_customer_id,
            provider_subscription_id="",
            plan=None,
            subscription_terms_version=None,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        BillingAccountService(session).billing_account_for(
            provider, user_id=user_id, workspace_id=workspace_id
        )
        lots = list(
            session.scalars(
                select(BillingCreditLotTable).where(BillingCreditLotTable.user_id == user_id)
            )
        )
        assert [(lot.id, lot.amount_nanos, lot.effective_at, lot.expires_at) for lot in lots] == [
            original
        ]
        period = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_ENDED_AT
        )
        assert period is not None and period.allowance_nanos == 0


def test_upgrading_swaps_the_plan_price_and_resizes_one_grant(
    isolated_services: ApiServices,
) -> None:
    """An upgrade changes what the account holds; it never buys a second of anything.

    Four things have money on them and each is asserted. A second subscription
    would carry the same three metered prices, so the account's usage would be
    counted onto two invoices. A second live grant would be an allowance given
    away twice. A period replaced rather than re-termed would hand back the spend
    already counted against it. And the cycle must not move, because the invoice
    the customer is part-way through is the one this usage belongs on.

    Upgrading twice must change nothing twice: two clicks on one button is the
    ordinary case, not the unlikely one.
    """

    provider = _Provider()
    user_id, workspace_id = carded_account(isolated_services.context)

    with isolated_services.context.database.session() as session:
        BillingAccountService(session).billing_account_for(
            provider, user_id=user_id, workspace_id=workspace_id
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        free = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )
        assert free is not None
        assert free.started_at == CYCLE_STARTED_AT
        assert free.ended_at == CYCLE_ENDED_AT
        assert free.allowance_nanos == FREE_PLAN_INCLUDED_NANOS
        # Spent while the account was free, and it has to survive the upgrade:
        # the usage is on the same invoice the prorated plan fee lands on.
        BillingAllowanceRepository(session).increment(
            user_id=user_id, at=CYCLE_STARTED_AT, cost_nanos=2_000_000_000
        )
        session.commit()

    upgraded = _plan_changes(isolated_services, provider).change_plan(
        user_id=user_id,
        target=BillingPlanId.Team,
        target_terms_version=published_plan(BillingPlanId.Team).terms_version,
    )

    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
        allowance = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )

    assert account is not None
    assert account.plan is BillingPlanId.Team
    assert account.provider_subscription_id == upgraded.provider_subscription_id == "sub_1"
    assert allowance is not None
    assert allowance.started_at == CYCLE_STARTED_AT
    assert allowance.ended_at == CYCLE_ENDED_AT
    assert allowance.allowance_nanos == TEAM_PLAN_INCLUDED_NANOS
    assert allowance.spent_nanos == 2_000_000_000
    assert provider.subscriptions == [BillingPlanId.Free]
    assert provider.plan_changes == [BillingPlanId.Team]
    with isolated_services.context.database.session() as session:
        assert (
            BillingCreditRepository(session).subscription_issued(
                user_id=user_id, period_ended_at=CYCLE_ENDED_AT
            )
            == TEAM_PLAN_INCLUDED_NANOS
        )

    again = _plan_changes(isolated_services, provider).change_plan(
        user_id=user_id,
        target=BillingPlanId.Team,
        target_terms_version=published_plan(BillingPlanId.Team).terms_version,
    )

    assert again.provider_subscription_id == "sub_1"
    assert provider.subscriptions == [BillingPlanId.Free]
    assert provider.plan_changes == [BillingPlanId.Team]
    with isolated_services.context.database.session() as session:
        assert (
            BillingCreditRepository(session).subscription_issued(
                user_id=user_id, period_ended_at=CYCLE_ENDED_AT
            )
            == TEAM_PLAN_INCLUDED_NANOS
        )


def test_paid_plan_credit_waits_for_invoice_payment_and_recovers_the_existing_intent(
    isolated_services: ApiServices,
) -> None:
    provider = _Provider(invoice_paid=False)
    user_id, workspace_id = carded_account(isolated_services.context)
    with isolated_services.context.database.session() as session:
        BillingAccountService(session).billing_account_for(
            provider, user_id=user_id, workspace_id=workspace_id
        )
    changes = _plan_changes(isolated_services, provider)
    with pytest.raises(UpstreamUnavailableError, match="matching paid invoice"):
        changes.change_plan(
            user_id=user_id,
            target=BillingPlanId.Team,
            target_terms_version=published_plan(BillingPlanId.Team).terms_version,
        )
    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
        assert account is not None and account.plan is BillingPlanId.Free
        assert (
            BillingCreditRepository(session).subscription_issued(
                user_id=user_id, period_ended_at=CYCLE_ENDED_AT
            )
            == FREE_PLAN_INCLUDED_NANOS
        )
    provider.invoice_paid = True
    recovered = changes.settle_open(now=utc_now() + timedelta(hours=1))
    assert recovered.applied_count == 1
    with isolated_services.context.database.session() as session:
        assert (
            BillingCreditRepository(session).subscription_issued(
                user_id=user_id, period_ended_at=CYCLE_ENDED_AT
            )
            == TEAM_PLAN_INCLUDED_NANOS
        )
    assert provider.plan_changes == [BillingPlanId.Team]


def test_an_upgrade_after_the_cycle_rolled_leaves_the_grant_funding_that_invoice(
    isolated_services: ApiServices,
) -> None:
    """The grant an upgrade replaces is this cycle's, never the last one's.

    A period ends at the provider and its invoice finalizes about an hour later,
    and an upgrade can land in between. What the account still names then is the
    grant bought for the period that just closed — the one that invoice is about
    to be paid out of. Expiring it would take back an allowance the customer had
    already been given and is about to be charged against, and the upgrade would
    be a bill for usage they had already paid for once.

    Nothing about the two cases differs at the call site, so the period is what
    tells them apart: the cycle the provider answers with is one nothing has
    opened here, and opening a cycle leaves the outgoing grant alone.
    """

    provider = _Provider()
    user_id, workspace_id = carded_account(isolated_services.context)

    with isolated_services.context.database.session() as session:
        BillingAccountService(session).billing_account_for(
            provider, user_id=user_id, workspace_id=workspace_id
        )
        session.commit()

    provider.cycle_started_at = CYCLE_ENDED_AT
    provider.cycle_ended_at = CYCLE_ENDED_AT + (CYCLE_ENDED_AT - CYCLE_STARTED_AT)

    _plan_changes(isolated_services, provider).change_plan(
        user_id=user_id,
        target=BillingPlanId.Team,
        target_terms_version=published_plan(BillingPlanId.Team).terms_version,
    )

    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
        closing = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )
        opened = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_ENDED_AT
        )

    assert account is not None
    with isolated_services.context.database.session() as session:
        credits = BillingCreditRepository(session)
        assert (
            credits.subscription_issued(user_id=user_id, period_ended_at=CYCLE_ENDED_AT)
            == FREE_PLAN_INCLUDED_NANOS
        )
        assert (
            credits.subscription_issued(user_id=user_id, period_ended_at=provider.cycle_ended_at)
            == TEAM_PLAN_INCLUDED_NANOS
        )
    # The closed period keeps the free terms it was granted and spent against,
    # and the new one opens on Team's.
    assert closing is not None
    assert closing.allowance_nanos == FREE_PLAN_INCLUDED_NANOS
    assert opened is not None
    assert opened.started_at == CYCLE_ENDED_AT
    assert opened.allowance_nanos == TEAM_PLAN_INCLUDED_NANOS


def test_an_account_with_no_subscription_cannot_start_work(
    isolated_services: ApiServices,
) -> None:
    """Refused, because its usage has nowhere to be billed.

    Admission asks whether what this runs will reach an invoice, and an account
    holding no subscription answers no: the ledger records the cost, the meter
    event leaves for the provider, and there is no subscription carrying the
    metered prices for either of them to land on. That is unbounded compute
    nobody is charged for, and it is what a workspace whose owner has never been
    provisioned, or whose subscription the provider says has ended, looks like.

    Sign-in provisions before it mints a session, so this is a state nothing
    should reach. The refusal is what makes that a fact rather than a hope.
    """

    user_id, workspace_id = unbilled_account(isolated_services.context)
    admission = DatabaseBillingAdmission()

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError, match="no subscription"),
    ):
        admission.admit_container_start(session, workspace_id=workspace_id, gpu=(), gpu_count=0)

    # A registration that stopped after the customer is the same answer: half of
    # what provisioning writes is not somewhere work may start from.
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_halfway",
            provider_subscription_id="",
            plan=None,
            subscription_terms_version=None,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        session.commit()

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError, match="no subscription"),
    ):
        admission.admit_container_start(session, workspace_id=workspace_id, gpu=(), gpu_count=0)


def test_the_container_limit_counts_every_workspace_the_account_owns(
    isolated_services: ApiServices,
) -> None:
    """The ceiling is a term of a plan, so it is the payer's, not a workspace's.

    Counted per workspace it would not be a ceiling at all: making another
    workspace is self-serve, so an account wanting twice the limit would click
    twice. Both workspaces below belong to one owner, and the containers in them
    add up against the one figure that owner's terms allow.

    Refused as a capacity conflict rather than a payment problem. This account
    owes nothing — paying would not raise the limit, and telling somebody their
    card is the issue sends them to fix something that is not broken.
    """

    first_workspace_id = owned_workspace(
        isolated_services.control_plane_service, f"first-{uuid4()}"
    ).id
    user_id = workspace_owner_user_id(isolated_services.context, first_workspace_id)
    with isolated_services.context.database.session() as session:
        second_workspace_id = WorkspaceRepository(session).create(name=f"second-{uuid4()}").id
        WorkspaceMemberRepository(session).ensure_owner(
            workspace_id=second_workspace_id, user_id=user_id
        )

    admission = DatabaseBillingAdmission()
    with isolated_services.context.database.session() as session:
        admission.admit_container_start(
            session, workspace_id=first_workspace_id, gpu=(), gpu_count=0
        )

    # Split across both workspaces, and one of them is only queued: a container
    # waiting for a worker has already been promised the capacity it asked for.
    with isolated_services.context.database.session() as session:
        for index in range(NO_CARD_MAX_CPU_CONTAINERS):
            ContainerRepository(session).upsert(
                ContainerRecord(
                    id=str(uuid4()),
                    name=f"held-{index}",
                    image="",
                    command=[],
                    workspace_id=(first_workspace_id if index % 2 == 0 else second_workspace_id),
                    status=(ContainerStatus.Pending if index == 0 else ContainerStatus.Running),
                )
            )
        session.commit()

    with isolated_services.context.database.session() as session:
        for workspace_id in (first_workspace_id, second_workspace_id):
            with pytest.raises(CapacityLimitReachedError):
                admission.admit_container_start(
                    session, workspace_id=workspace_id, gpu=(), gpu_count=0
                )
