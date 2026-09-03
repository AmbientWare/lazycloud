from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.identity import (
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import (
    FREE_PLAN_INCLUDED_NANOS,
    NO_CARD_INCLUDED_NANOS,
    NO_CARD_MAX_CPU_CONTAINERS,
    TEAM_PLAN_INCLUDED_NANOS,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import CapacityLimitReachedError, PaymentRequiredError
from shared.identity import WorkspaceRole
from shared.payments import (
    HostedPaymentSession,
    PaymentCustomer,
    ProviderCreditGrant,
    ProviderInvoice,
    ProviderSubscription,
    SubscriptionProration,
)
from shared.timestamps import utc_now
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
    grants: list[int] = field(default_factory=list)
    grant_predecessors: list[datetime | None] = field(default_factory=list)
    """The cycle each grant was told it follows, in the order they were bought.

    What decides when an allowance becomes spendable, so it is money: a grant
    told it follows a cycle is held back until that cycle's invoice has settled,
    and one told it follows nothing is spendable at once."""

    expired_grants: list[str] = field(default_factory=list)
    plan: BillingPlanId = BillingPlanId.Free
    cycle_started_at: datetime = CYCLE_STARTED_AT
    cycle_ended_at: datetime = CYCLE_ENDED_AT
    """The cycle the provider currently says the subscription is in.

    Movable, because a renewal moves it and a plan change does not, and telling
    those two apart is what decides whether an outgoing grant is expired."""

    cards_on_file: set[str] = field(default_factory=set)
    """Customers the provider says hold something chargeable."""

    @property
    def live_grants(self) -> list[str]:
        """Every grant issued that has not since been expired."""

        return [
            f"credgr_{index}"
            for index in range(1, len(self.grants) + 1)
            if f"credgr_{index}" not in self.expired_grants
        ]

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
        )

    def set_subscription_plan(
        self,
        *,
        provider_subscription_id: str,
        plan: BillingPlanId,
        proration: SubscriptionProration,
    ) -> ProviderSubscription:
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
        )

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        raise AssertionError("subscribing already holds what the provider returned")

    def create_credit_grant(
        self,
        *,
        account_id: str,
        provider_customer_id: str,
        amount_nanos: int,
        period_ended_at: datetime,
        previous_period_ended_at: datetime | None,
    ) -> ProviderCreditGrant:
        del account_id, provider_customer_id
        self.grants.append(amount_nanos)
        self.grant_predecessors.append(previous_period_ended_at)
        return ProviderCreditGrant(
            provider_credit_grant_id=f"credgr_{len(self.grants)}",
            amount_nanos=amount_nanos,
            expires_at=period_ended_at,
        )

    def expire_credit_grant(self, *, provider_credit_grant_id: str) -> None:
        self.expired_grants.append(provider_credit_grant_id)

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        raise AssertionError("subscribing must not read invoices")

    def invoices_for(
        self, *, provider_customer_id: str, since: datetime, limit: int = 12
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
            provider_credit_grant_id="credgr_paying",
            plan=BillingPlanId.Team,
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


def test_provisioning_an_account_twice_leaves_one_customer_and_one_subscription(
    isolated_services: ApiServices,
) -> None:
    """A sign-in that already provisioned its account must not do it again.

    Provisioning happens on every sign-in, so the second one arrives at an
    account that already holds everything — and so does the retry after a sign-in
    that failed once the provider had already answered. A second customer would
    hold one person's invoices with only one of them named here; a second
    subscription would carry the same three metered prices, and that account's
    usage would be counted onto two invoices.
    """

    provider = _Provider()
    user_id, workspace_id = unbilled_account(isolated_services.context)

    with isolated_services.context.database.session() as session:
        first = BillingAccountService(session).billing_account_for(
            provider, user_id=user_id, workspace_id=workspace_id
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        again = BillingAccountService(session).billing_account_for(
            provider, user_id=user_id, workspace_id=workspace_id
        )
        session.commit()
        stored = BillingAccountRepository(session).get_by_user(user_id)

    assert stored is not None
    assert first.provider_customer_id == again.provider_customer_id == "cus_subscriber"
    assert first.provider_subscription_id == again.provider_subscription_id == "sub_1"
    assert provider.customers == [workspace_id]
    assert provider.subscriptions == [BillingPlanId.Free]
    # The cardless figure, not the free plan's: signing in provisions a
    # subscription before anybody has said how they will pay, and what a plan
    # includes is credit against a bill nobody can raise yet.
    assert provider.grants == [NO_CARD_INCLUDED_NANOS]


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
        user_id=user_id, target=BillingPlanId.Team
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
    assert provider.grants == [FREE_PLAN_INCLUDED_NANOS, TEAM_PLAN_INCLUDED_NANOS]
    assert provider.expired_grants == ["credgr_1"]
    assert provider.live_grants == [account.provider_credit_grant_id] == ["credgr_2"]
    # Neither allowance follows a cycle, because this account has held only one.
    # Held back for a predecessor that does not exist, the plan somebody just
    # paid for would include nothing until three days after they bought it.
    assert provider.grant_predecessors == [None, None]

    again = _plan_changes(isolated_services, provider).change_plan(
        user_id=user_id, target=BillingPlanId.Team
    )

    assert again.provider_subscription_id == "sub_1"
    assert provider.subscriptions == [BillingPlanId.Free]
    assert provider.plan_changes == [BillingPlanId.Team]
    assert provider.grants == [FREE_PLAN_INCLUDED_NANOS, TEAM_PLAN_INCLUDED_NANOS]
    assert provider.expired_grants == ["credgr_1"]


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
        user_id=user_id, target=BillingPlanId.Team
    )

    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
        closing = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )
        opened = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_ENDED_AT
        )

    assert provider.expired_grants == []
    assert account is not None
    assert provider.live_grants == ["credgr_1", "credgr_2"]
    assert account.provider_credit_grant_id == "credgr_2"
    # Both grants are live at once here, which is the whole hazard: the second is
    # bought for the cycle that opened at the seam and must stay out of reach
    # until the invoice the first one funds has been settled.
    assert provider.grant_predecessors == [None, CYCLE_ENDED_AT]
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
            provider_credit_grant_id="",
            plan=None,
        )
        session.commit()

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError, match="no subscription"),
    ):
        admission.admit_container_start(session, workspace_id=workspace_id, gpu=(), gpu_count=0)


def test_an_account_with_a_card_is_admitted_past_its_allowance(
    isolated_services: ApiServices,
) -> None:
    """Spending past what a plan includes is billed, never refused.

    The card gate must not become a spend cap on customers who can be charged.
    Overage is metered, invoiced and chased through the card on file — refusing
    it would stop paying customers at a ceiling nobody agreed to, which is the
    opposite of what a platform selling scalable compute is for.

    The same account with no card is refused on the identical numbers, so what
    separates the two is only whether anybody can be billed.
    """

    carded_user_id, carded_workspace_id = carded_account(isolated_services.context)
    cardless_user_id, cardless_workspace_id = unbilled_account(isolated_services.context)
    with isolated_services.context.database.session() as session:
        BillingAccountService(session).billing_account_for(
            _Provider(), user_id=carded_user_id, workspace_id=carded_workspace_id
        )
        # Written rather than provisioned: the fake names one customer for every
        # account it registers, and two of those collide on the index that keeps
        # a provider customer belonging to exactly one account.
        BillingAccountRepository(session).upsert(
            user_id=cardless_user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_{cardless_user_id}",
            provider_subscription_id=f"sub_{cardless_user_id}",
            provider_credit_grant_id=f"credgr_{cardless_user_id}",
            plan=BillingPlanId.Free,
        )
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=cardless_user_id,
            period_started_at=CYCLE_STARTED_AT,
            period_ended_at=CYCLE_ENDED_AT,
            allowance_nanos=NO_CARD_INCLUDED_NANOS,
            funded=True,
        )
        session.commit()

    # Both spend every nanodollar their cycle came with, and then some.
    now = utc_now()
    for user_id in (carded_user_id, cardless_user_id):
        with isolated_services.context.database.session() as session:
            period = BillingAllowanceRepository(session).current_period(user_id=user_id, at=now)
            assert period is not None
            BillingAllowanceRepository(session).increment(
                user_id=user_id,
                at=now,
                cost_nanos=period.allowance_nanos + 1,
            )
            session.commit()

    admission = DatabaseBillingAdmission()
    with isolated_services.context.database.session() as session:
        admission.admit_container_start(
            session, workspace_id=carded_workspace_id, gpu=(), gpu_count=0
        )
        with pytest.raises(PaymentRequiredError):
            admission.admit_container_start(
                session, workspace_id=cardless_workspace_id, gpu=(), gpu_count=0
            )


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

    user_id, first_workspace_id = unbilled_account(isolated_services.context)
    with isolated_services.context.database.session() as session:
        second_workspace_id = WorkspaceRepository(session).create(name=f"second-{uuid4()}").id
        WorkspaceMemberRepository(session).ensure_owner(
            workspace_id=second_workspace_id, user_id=user_id
        )
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_{user_id}",
            provider_subscription_id=f"sub_{user_id}",
            provider_credit_grant_id=f"credgr_{user_id}",
            plan=BillingPlanId.Free,
        )
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=user_id,
            period_started_at=CYCLE_STARTED_AT,
            period_ended_at=CYCLE_ENDED_AT,
            allowance_nanos=NO_CARD_INCLUDED_NANOS,
            funded=True,
        )
        session.commit()

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
