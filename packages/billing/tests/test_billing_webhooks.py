from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from api.server.services import ApiServices
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import (
    published_plan,
    subscription_terms,
)
from shared.errors import PaymentRequiredError
from shared.payments import (
    HostedPaymentSession,
    PaymentCustomer,
    PaymentEvent,
    ProviderInvoice,
    ProviderPaidSubscriptionPeriod,
    ProviderSubscription,
    SubscriptionChangeTiming,
)
from tests.domain_fixtures import unfunded_billing_account, workspace_owner_user_id

from billing import BillingWebhookService, DatabaseBillingAdmission

CYCLE_STARTED_AT = datetime(2026, 8, 1, tzinfo=UTC)
CYCLE_ENDED_AT = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass(slots=True)
class _Provider:
    """A payment provider that answers about the cards it holds."""

    default_payment_methods: dict[str, str] = field(default_factory=dict)
    payment_method_owners: dict[str, str] = field(default_factory=dict)
    """Which customer each saved card currently belongs to, as the provider would
    answer it — the read-back that stops a retried notification about a replaced
    card from putting the old one back."""

    terms_version: SubscriptionTermsVersion = SubscriptionTermsVersion.Team

    @property
    def subscription_plan(self) -> BillingPlanId:
        return subscription_terms(self.terms_version).plan

    cards_on_file: set[str] = field(default_factory=set)
    """Customers the provider says hold something chargeable."""

    def create_customer(self, *, account_id: str, email: str, workspace_id: str) -> PaymentCustomer:
        raise AssertionError("applying a delivery must not create customers")

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

    def has_payment_method(self, *, provider_customer_id: str) -> bool:
        return provider_customer_id in self.cards_on_file

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        self.default_payment_methods[provider_customer_id] = provider_payment_method_id

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
        raise AssertionError("applying a delivery must not meter usage")

    subscription_status: str = "active"
    """What the provider says the subscription is, read back rather than taken
    from the delivery — the whole reason a standing delivery is believed about
    nothing."""

    def create_subscription(
        self, *, provider_customer_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        raise AssertionError("applying a delivery must not subscribe anyone")

    def set_subscription_plan(
        self,
        *,
        provider_subscription_id: str,
        terms_version: SubscriptionTermsVersion,
        timing: SubscriptionChangeTiming,
        operation_id: str,
        operation_created_at: datetime,
    ) -> ProviderSubscription:
        raise AssertionError("applying a delivery must not change anyone's plan")

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        return ProviderSubscription(
            provider_subscription_id=provider_subscription_id,
            status=self.subscription_status,
            current_period_started_at=CYCLE_STARTED_AT,
            current_period_ended_at=CYCLE_ENDED_AT,
            plan=self.subscription_plan,
            terms_version=self.terms_version,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        raise AssertionError("applying a card delivery must not read invoices")

    def paid_subscription_periods(
        self, *, provider_customer_id: str, provider_subscription_id: str, since: datetime
    ) -> Sequence[ProviderPaidSubscriptionPeriod]:
        terms = subscription_terms(self.terms_version)
        if terms.monthly_nanos == 0:
            return ()
        return (
            ProviderPaidSubscriptionPeriod(
                provider_invoice_id="in_plan_paid",
                provider_invoice_line_id="il_plan_paid",
                provider_subscription_id=provider_subscription_id,
                plan=terms.plan,
                terms_version=terms.version,
                period_started_at=CYCLE_STARTED_AT,
                period_ended_at=CYCLE_ENDED_AT,
                prorated=False,
                amount_nanos=terms.monthly_nanos,
                invoice_paid_nanos=terms.monthly_nanos,
                paid_at=CYCLE_STARTED_AT,
            ),
        )

    def invoices_for(
        self, *, provider_customer_id: str, since: datetime, limit: int | None = 12
    ) -> Sequence[ProviderInvoice]:
        raise AssertionError("applying a delivery must not list invoices")


def test_a_saved_card_becomes_the_one_charges_are_taken_from(
    isolated_services: ApiServices,
) -> None:
    """Saving a card does not make it the default, and nothing does it implicitly.

    A customer who completed the hosted page and had this step skipped is
    indistinguishable from one who never saved a card at all — until the charge
    is refused for want of a default. There is no other moment this platform
    would know to look: the customer leaves for the provider's page and may never
    return to the one that sent them.
    """

    provider = _Provider()
    provider.payment_method_owners = {"pm_saved": "cus_webhook", "pm_theirs": "cus_someone_else"}
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_webhook",
            provider_subscription_id="",
            plan=None,
            subscription_terms_version=None,
            scheduled_terms_version=None,
            scheduled_change_at=None,
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
    # put the old card back and charge them on it.
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


def test_a_failed_payment_leaves_the_account_admission_refuses(
    isolated_services: ApiServices,
) -> None:
    """`past_due` is written by a delivery and read by admission, or by nobody.

    An account is refused on one thing only — the provider saying a
    payment did not go through — because the provider bills its overage and
    chases its own card. Without this path that status has no writer, and a
    customer whose card has failed for a month goes on starting work nobody can
    collect for.

    The delivery is a cue, not a claim: the subscription is read back, so the
    same event arriving after the customer has paid finds `active` and clears the
    standing rather than reinstating a refusal that no longer holds.
    """

    provider = _Provider()
    provider.subscription_status = "past_due"
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_webhook",
            provider_subscription_id="sub_webhook",
            plan=BillingPlanId.Team,
            subscription_terms_version=published_plan(BillingPlanId.Team).terms_version,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        assert BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_invoice_failed",
                type="invoice.payment_failed",
                object_id="in_1",
                customer_id="cus_webhook",
            )
        )
        session.commit()

    # A payment that failed buys nothing and voids nothing: the terms of the
    # cycle the customer is part-way through are not what went wrong.

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError),
    ):
        DatabaseBillingAdmission().admit_container_start(
            session, workspace_id=workspace_id, gpu=(), gpu_count=0
        )


def test_a_plan_changed_at_the_provider_leaves_one_grant_over_the_cycle(
    isolated_services: ApiServices,
) -> None:
    """A plan change is a plan change whichever door it arrives through.

    Somebody moves plan on the provider's own portal, or a subscribe dies after
    the price swap and before it is recorded. Either way the delivery is the
    first this platform hears of it, and the cycle it names is the one already
    open: re-terming it and buying the larger allowance while the smaller one is
    still live would give the customer both, and nothing on the invoice says
    which of the two a charge was taken from.

    A renewal reaching the same code must not be treated this way, which is why
    the decision is read off the period rather than off the delivery.
    """

    provider = _Provider()
    provider.terms_version = SubscriptionTermsVersion.TeamLegacy
    provider.cards_on_file.add("cus_webhook")
    user_id, _ = unfunded_billing_account(
        isolated_services.context,
        period_started_at=CYCLE_STARTED_AT,
        period_ended_at=CYCLE_ENDED_AT,
    )
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_webhook",
            provider_subscription_id="sub_webhook",
            plan=BillingPlanId.Free,
            subscription_terms_version=SubscriptionTermsVersion.FreeLegacy,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        # Carded: a plan's own terms are what an account gets once somebody can be
        # charged past them, so without this the cycle would be re-termed onto the
        # figure the platform gives away rather than onto Team's.
        BillingAccountRepository(session).set_payment_method_present(
            user_id=user_id, present=True, at=CYCLE_STARTED_AT
        )
        # The cycle the account is part-way through, on the free plan's terms.
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=user_id,
            period_started_at=CYCLE_STARTED_AT,
            period_ended_at=CYCLE_ENDED_AT,
            allowance_nanos=subscription_terms(SubscriptionTermsVersion.FreeLegacy).included_nanos,
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        assert BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_plan_changed",
                type="customer.subscription.updated",
                object_id="sub_webhook",
                customer_id="cus_webhook",
            )
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
        allowance = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )

    assert account is not None
    assert account.plan is BillingPlanId.Team
    assert allowance is not None
    assert allowance.started_at == CYCLE_STARTED_AT
    assert (
        allowance.allowance_nanos
        == subscription_terms(SubscriptionTermsVersion.TeamLegacy).included_nanos
    )


def test_a_subscription_that_ends_leaves_an_account_on_no_plan_and_refused(
    isolated_services: ApiServices,
) -> None:
    """A cancellation clears the plan as well as the subscription.

    They say one thing together: what this account is on. A row that kept its
    plan would report the terms of a subscription that no longer exists, offer
    the customer no way back onto one — the dashboard hides subscribing from
    anybody already on Team — and satisfy admission while every meter event it
    sends lands on no invoice.

    The standing is not touched, because a subscription ending says nothing about
    whether the money it already owed was collected.
    """

    provider = _Provider()
    provider.subscription_status = "canceled"
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_webhook",
            provider_subscription_id="sub_webhook",
            plan=BillingPlanId.Team,
            subscription_terms_version=published_plan(BillingPlanId.Team).terms_version,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        assert BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_subscription_deleted",
                type="customer.subscription.deleted",
                object_id="sub_webhook",
                customer_id="cus_webhook",
            )
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)

    assert account is not None
    assert account.plan is None
    assert account.provider_subscription_id == ""
    assert account.status is BillingAccountStatus.Active
    # Left naming the grant, which funds the final invoice this cancellation
    # raises for the part-cycle it ends.

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError, match="no subscription"),
    ):
        DatabaseBillingAdmission().admit_container_start(
            session, workspace_id=workspace_id, gpu=(), gpu_count=0
        )


def test_a_saved_card_preserves_existing_credit_without_replenishment(
    isolated_services: ApiServices,
) -> None:

    provider = _Provider()
    provider.terms_version = SubscriptionTermsVersion.Free
    provider.payment_method_owners = {"pm_first": "cus_webhook"}
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)

    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_webhook",
            provider_subscription_id="sub_webhook",
            plan=BillingPlanId.Free,
            subscription_terms_version=published_plan(BillingPlanId.Free).terms_version,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=user_id,
            period_started_at=CYCLE_STARTED_AT,
            period_ended_at=CYCLE_ENDED_AT,
            allowance_nanos=1_000_000_000,
        )
        BillingAllowanceRepository(session).increment(
            user_id=user_id, at=CYCLE_STARTED_AT, cost_nanos=400_000_000
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        assert BillingWebhookService(session, lambda: provider).apply(
            event=PaymentEvent(
                id="evt_first_card",
                type="payment_method.attached",
                object_id="pm_first",
                customer_id="cus_webhook",
                payment_method_id="pm_first",
            )
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
        allowance = BillingAllowanceRepository(session).current_period(
            user_id=user_id, at=CYCLE_STARTED_AT
        )

    assert account is not None
    assert account.payment_method_attached_at is not None
    assert allowance is not None
    assert allowance.allowance_nanos == 1_000_000_000
    assert allowance.spent_nanos == 400_000_000
