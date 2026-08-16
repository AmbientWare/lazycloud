"""The prepared sandbox, and the account one billing run is allowed to bill.

Every scenario in this directory needs the same two things before it may create
anything: a credential that provably belongs to the account the operator named,
against a stack provably running the code under test; and an account of its own
to put a payment relationship on. Both live here because getting either wrong is
money on somebody else's record — a subscription bought in the wrong Stripe
account, or a plan attached to a person who never asked for one.

Nothing here decides anything about billing. It refuses, or it hands back the
prepared environment and a run-scoped account for the scenario to drive through
the product's own routes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_rates import ComputeRateRepository, PlatformRateRepository
from gateway.settings import GatewaySettings
from lazycloud.config import get_profile
from provider_stripe import StripeBilling, StripeCatalog, StripeSettings
from provider_stripe.api import StripeObject, read
from shared.billing_accounts import BillingAccount
from shared.billing_plans import BillingPlanId
from shared.billing_quotes import ContainerShape, LedgerComponent
from shared.billing_rate_card import PUBLISHED_PLANS
from shared.http.billing import BillingAllowanceResponse, BillingSummaryResponse
from shared.http.system import TokenCreateRequest, TokenCreateResponse
from shared.http.users import UserCreateRequest, UserResponse
from shared.http.workspaces import WorkspaceCreateRequest, WorkspaceResponse
from shared.http_transport import HttpChannel
from shared.identity import PlatformRole
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner
from tests.e2e._support.process import LivePrerequisiteError

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

STRIPE_API_KEY_ENV = "LAZYCLOUD_STRIPE_API_KEY"
DATABASE_URL_ENV = "LAZYCLOUD_DATABASE_URL"

SUBSCRIBE_ROUTE = "/api/v1/billing/subscription"
CARD_SESSION_ROUTE = "/api/v1/billing/card-session"

TEST_PAYMENT_METHOD = "pm_card_visa"
"""Stripe's own documented test card, which exists only in a sandbox."""

REPLACEMENT_PAYMENT_METHOD = "pm_card_mastercard"
"""A second test card, so "which card is the default" can tell two states apart."""

FAILING_PAYMENT_METHOD = "pm_card_chargeCustomerFail"
"""Stripe's test card that attaches to a customer and then refuses every charge.

The only way to produce a real failed payment without waiting for a renewal:
the card is saved exactly as a working one is, and the refusal happens where a
real one does — at the charge, on Stripe's side, reported back by their own
delivery.
"""

ADMIN_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class BillingGate:
    """The prepared environment, once every prerequisite has been met."""

    account_id: str
    endpoint: str
    public_url: str
    """Where the platform will let the provider send a customer back to.

    Read from the deployment setting the control plane itself reads, because the
    card route refuses any return address that is not on this platform's own
    origin — an open redirect on the page after a card is saved being the one
    worth abusing.
    """

    admin: HttpChannel
    provider: StripeBilling
    database: DatabaseClient

    @property
    def client(self) -> httpx.Client:
        return self.provider.client

    def close(self) -> None:
        self.provider.client.close()
        self.database.dispose()


@dataclass(slots=True)
class RunAccount:
    """The person, workspace and credential one run created and will delete."""

    suffix: str
    user_id: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    token: str = ""

    def channel(self, gate: BillingGate) -> HttpChannel:
        return HttpChannel(
            endpoint=gate.endpoint,
            token=self.token,
            timeout_seconds=ADMIN_TIMEOUT_SECONDS,
        )


def billing_gate(*, live: bool, confirm_account: str) -> BillingGate:
    """Refuse unless everything a run needs is present and points where it should.

    Ordered so nothing expensive is checked before the cheap refusals, and so the
    account the credential belongs to is established before the operator's
    confirmation is compared against it: a run told the wrong account has to be
    told which one it actually holds.
    """

    if not live:
        raise LivePrerequisiteError("this scenario requires the explicit --live opt-in")
    for name in (STRIPE_API_KEY_ENV, DATABASE_URL_ENV):
        if not os.getenv(name, "").strip():
            raise LivePrerequisiteError(f"{name} is required to reach the account under test")
    settings = StripeSettings()
    # Before anything is read, let alone written. These scenarios attach the
    # provider's documented test cards, subscribe accounts and drive charges
    # through to settlement; against a live credential that is real customers
    # and real money, and every other guard here is about *which account* rather
    # than which kind. The credential is read from the same variable the control
    # plane uses, so an operator shell that happens to hold the live one is not
    # an exotic mistake — it is the ordinary way this would go wrong.
    if settings.live_mode:
        raise LivePrerequisiteError(
            f"{STRIPE_API_KEY_ENV} holds a live credential; these scenarios attach test "
            "cards and charge real customers with one. Point it at test data first"
        )
    provider = settings.provider()
    catalog = StripeCatalog(client=provider.client)
    account_id = catalog.account_id()
    if not confirm_account:
        raise LivePrerequisiteError(
            f"--confirm-account is required; the credential in hand belongs to {account_id}"
        )
    if confirm_account != account_id:
        raise LivePrerequisiteError(
            f"the credential in hand belongs to {account_id}, not {confirm_account}"
        )
    # Every plan, not only the one a scenario upgrades to: an account is put on
    # the free plan the moment it reaches a billing surface, so a missing free
    # price is a run that cannot register a customer at all.
    missing = catalog.published(
        plan_prices={plan.id: plan.monthly_nanos for plan in PUBLISHED_PLANS}
    ).missing
    if missing:
        raise LivePrerequisiteError(
            "the catalog is not published in this account; run `lazycloud-admin billing "
            "publish-catalog`. Missing: " + ", ".join(entry.name for entry in missing)
        )
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin)
    )
    _require_published_rates(database)
    profile = get_profile()
    if not profile.token:
        raise LivePrerequisiteError(
            "the active lazycloud profile has no token; this scenario needs an administrator"
        )
    endpoint = profile.resolved_endpoint()
    _require_code_under_test(endpoint)
    return BillingGate(
        account_id=account_id,
        endpoint=endpoint,
        public_url=GatewaySettings().public_http_url,
        admin=HttpChannel(
            endpoint=endpoint,
            token=profile.token,
            timeout_seconds=ADMIN_TIMEOUT_SECONDS,
        ),
        provider=provider,
        database=database,
    )


def create_run_account(gate: BillingGate, run: RunAccount) -> None:
    """One account and one workspace that exist only for this run.

    An account is what a payment relationship attaches to, so a run that
    subscribed an existing person would put a real subscription on their record
    and could not clean it up without cancelling theirs. It is created as an
    administrator because creating a workspace is an administrator's operation
    and the workspace has to be owned by the account the ledger will bill.
    """

    user = UserResponse.model_validate(
        gate.admin.post(
            "/api/v1/users",
            UserCreateRequest(
                display_name=f"e2e-billing-{run.suffix}",
                role=PlatformRole.Administrator,
            ).model_dump(mode="json"),
        )
    )
    run.user_id = user.id
    minted = TokenCreateResponse.model_validate(
        gate.admin.post(
            f"/api/v1/users/{user.id}/tokens",
            TokenCreateRequest(name=f"e2e-billing-{run.suffix}").model_dump(mode="json"),
        )
    )
    run.token = minted.token
    workspace = WorkspaceResponse.model_validate(
        run.channel(gate).post(
            "/api/v1/workspaces",
            WorkspaceCreateRequest(name=f"e2e-billing-{run.suffix}").model_dump(mode="json"),
        )
    )
    run.workspace_id = workspace.id
    run.workspace_name = workspace.name


class _PaymentMethod(StripeObject):
    id: str = ""


def billing_account(gate: BillingGate, run: RunAccount) -> BillingAccount | None:
    with gate.database.session() as session:
        return BillingAccountRepository(session).get_by_user(run.user_id)


def register_customer(gate: BillingGate, run: RunAccount) -> BillingAccount:
    """Take the run's account through the surface that provisions it.

    The card route, because that is where an account that was minted rather than
    signed in acquires a payment relationship. Opening it provisions the whole
    shape — a customer at the provider, a subscription on the free plan carrying
    that plan's price and the three metered prices, the period that subscription's
    own cycle defines, and the grant that funds it — so what comes back is an
    account already able to be billed, and a scenario that later subscribes is
    changing a price rather than creating a relationship.
    """

    run.channel(gate).post(
        CARD_SESSION_ROUTE,
        {"return_url": gate.public_url, "cancel_url": gate.public_url},
    )
    account = billing_account(gate, run)
    if account is None or not account.provider_customer_id:
        raise RuntimeError("the card route registered no customer for the run's account")
    if not account.provider_subscription_id or account.plan is None:
        raise RuntimeError(
            "the card route left the run's account unprovisioned; it holds subscription "
            f"{account.provider_subscription_id or 'nothing'} on plan {account.plan}"
        )
    return account


def attach_card(gate: BillingGate, provider_customer_id: str, *, token: str) -> str:
    """Save a card at the provider, which is the only half a customer does here.

    Nothing about the default is decided here: which card the charges go to is
    the platform's own card-saved handler's to set, and a scenario watching that
    happen must not have done it first.
    """

    return read(
        _PaymentMethod,
        gate.client,
        "POST",
        f"/payment_methods/{token}/attach",
        data=[("customer", provider_customer_id)],
    ).id


def attach_default_card(
    gate: BillingGate, provider_customer_id: str, *, token: str = TEST_PAYMENT_METHOD
) -> str:
    """Put a working card on the customer, the way the product ends up with one.

    Making it the default is the adapter call the card-saved handler makes, so
    what comes back is a customer in the state the product leaves one in — what a
    scenario whose subject is something later needs before it starts.
    """

    method_id = attach_card(gate, provider_customer_id, token=token)
    gate.provider.set_default_payment_method(
        provider_customer_id=provider_customer_id,
        provider_payment_method_id=method_id,
    )
    return method_id


def plan_allowance(summary: BillingSummaryResponse) -> BillingAllowanceResponse:
    """The terms of the cycle this account is part-way through.

    Both levels are nullable and mean different things: no plan is an account on
    no subscription, and a plan with no allowance is the few minutes between a
    cycle ending and the renewal that opens the next one. A caller here has just
    put an account on a plan, so either is a failure.
    """

    if summary.plan is None:
        raise RuntimeError("the account is on no plan, so it has no allowance to report")
    if summary.plan.allowance is None:
        raise RuntimeError(
            f"the account is on {summary.plan.id.value} with no period covering this instant"
        )
    return summary.plan.allowance


def _require_published_rates(database: DatabaseClient) -> None:
    """Refuse before creating anything if usage metered now would price at nothing.

    Asked as the pricer asks it — a quote for the shape a run places, at this
    instant — rather than by counting rows, because a card published for next
    month is rows that exist and cover nothing.
    """

    now = utc_now()
    with database.session() as session:
        compute = ComputeRateRepository(session).quotes_for(
            shape=ContainerShape(
                billing_owner=UsageBillingOwner.PlatformFleet,
                gpu_type="",
                cpu_millicores=1_000,
                memory_mib=1_024,
                gpu_count=0,
            ),
            components=(
                LedgerComponent.ContainerTime,
                LedgerComponent.Cpu,
                LedgerComponent.Memory,
            ),
            started_at=now,
            ended_at=now,
        )
        platform = PlatformRateRepository(session).quotes_for(
            component=LedgerComponent.Egress,
            started_at=now,
            ended_at=now,
        )
    if not compute or not platform:
        raise LivePrerequisiteError(
            "no published rate covers this instant; run `lazycloud-admin billing publish-rates`"
        )


def _require_code_under_test(endpoint: str) -> None:
    """Establish the stack serves the code these scenarios exist to prove.

    A stack reports healthy while serving an image built from older source, and a
    pass against that proves nothing. Asked of the running process itself: the
    subscription route is the newest thing these runs depend on, and an
    unauthenticated call to it is refused for want of a credential where it
    exists and not found where it does not. Nothing is created either way.

    Carries the body the route actually takes, so what this establishes is the
    route these scenarios post to rather than one that happens to share its
    address — and so a body the running image would reject can never turn into a
    refusal this reads as an absence.
    """

    try:
        response = httpx.post(
            f"{endpoint}{SUBSCRIBE_ROUTE}",
            json={"plan": BillingPlanId.Team.value},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise LivePrerequisiteError(f"the control plane is unavailable: {endpoint}") from exc
    if response.status_code == httpx.codes.NOT_FOUND:
        raise LivePrerequisiteError(
            f"the running control plane serves no {SUBSCRIBE_ROUTE}; it is older than the "
            "subscription route these scenarios exist to prove"
        )
    if response.status_code not in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
        raise LivePrerequisiteError(
            f"{SUBSCRIBE_ROUTE} answered an unauthenticated call with "
            f"{response.status_code}, which is neither a refusal nor an absence"
        )


__all__ = [
    "ADMIN_TIMEOUT_SECONDS",
    "CARD_SESSION_ROUTE",
    "DATABASE_URL_ENV",
    "FAILING_PAYMENT_METHOD",
    "REPLACEMENT_PAYMENT_METHOD",
    "STRIPE_API_KEY_ENV",
    "SUBSCRIBE_ROUTE",
    "TEST_PAYMENT_METHOD",
    "BillingGate",
    "RunAccount",
    "attach_card",
    "attach_default_card",
    "billing_account",
    "billing_gate",
    "create_run_account",
    "plan_allowance",
    "register_customer",
]
