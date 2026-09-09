from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse
from uuid import UUID

from billing.costs import (
    MAX_COST_PAGE,
    BillingStanding,
    BillingStandingService,
    UsageCostSeries,
    UsageCostService,
)
from billing.funding import BillingFundingService
from billing.purchases import CreditPurchaseService
from database.repositories.billing_costs import PayerCostScope
from fastapi import APIRouter, Depends, Query, status
from shared.billing_rate_card import published_plan
from shared.credit_payments import CreditPurchaseKind
from shared.errors import InvalidInputError
from shared.funding import FundingBalance
from shared.http.billing import (
    BillingAccountAdminListResponse,
    BillingAccountAdminResponse,
    BillingAllowanceResponse,
    BillingComplimentaryRequest,
    BillingEntitlementUsageResponse,
    BillingHostedSessionRequest,
    BillingHostedSessionResponse,
    BillingPlanChangeRequest,
    BillingPlanResponse,
    BillingSummaryResponse,
    CreditBalanceResponse,
    CreditPurchaseRequest,
    CreditPurchaseResponse,
    CreditSummaryResponse,
    UsageBudgetResponse,
)
from shared.http.billing_preferences import BillingPreferences
from shared.http.pricing import PlanEntitlementsResponse
from shared.http.usage import (
    UsageCostBucket,
    UsageCostBucketResponse,
    UsageCostDimensionTotalResponse,
    UsageCostGroupKey,
    UsageCostListResponse,
    UsageCostSeriesResponse,
)
from shared.identity import PlatformRole, UserIdentityRecord, UserStatus
from shared.payments import BILLING_CURRENCY
from shared.timestamps import utc_now

from api.server.auth import admin_access, read_user, write_user
from api.server.dependencies import current_services
from api.server.routers.control_plane.users import user_response
from api.server.routers.resource_api.common import usage_cost_list_response
from api.server.services import ApiServices
from billing import (
    MAX_ACCOUNT_PAGE,
    RECENT_COST_WINDOW,
    AdministeredAccount,
    BillingAccountAdminService,
    BillingAccountService,
    BillingPlanChangeService,
    decode_account_cursor,
    owned_workspace_id,
)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


@router.get(
    "/preferences", response_model=BillingPreferences, operation_id="get_billing_preferences"
)
def get_billing_preferences(
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> BillingPreferences:
    with services.context.database.session() as session:
        return BillingFundingService(session).get_preferences(user_id=user_id)


@router.put(
    "/preferences", response_model=BillingPreferences, operation_id="set_billing_preferences"
)
def set_billing_preferences(
    request: BillingPreferences,
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> BillingPreferences:
    with services.context.database.session() as session:
        return BillingFundingService(session).set_preferences(user_id=user_id, preferences=request)


@router.get("/usage-budget", response_model=UsageBudgetResponse, operation_id="get_usage_budget")
def get_usage_budget(
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> UsageBudgetResponse:
    with services.context.database.session() as session:
        budget = BillingFundingService(session).usage_budget(user_id=user_id)
    return UsageBudgetResponse.model_validate(budget)


@router.get("/credits", response_model=CreditSummaryResponse, operation_id="get_credit_balance")
def get_credit_balance(
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> CreditSummaryResponse:
    with services.context.database.session() as session:
        summary = BillingStandingService(session).credit_balance(user_id=user_id, at=utc_now())
    return CreditSummaryResponse(
        ready=summary.ready,
        compute=_credit_balance_response(summary.compute),
        storage_and_transfer=_credit_balance_response(summary.storage_and_transfer),
    )


def _credit_balance_response(balance: FundingBalance) -> CreditBalanceResponse:
    return CreditBalanceResponse(
        purchased_nanos=balance.credits.purchased_nanos,
        subscription_nanos=balance.credits.subscription_nanos,
        trial_nanos=balance.credits.trial_nanos,
        held_nanos=balance.held_nanos,
        debt_nanos=balance.debt_nanos,
        available_nanos=balance.available_nanos,
    )


@router.post(
    "/credit-purchases",
    response_model=CreditPurchaseResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_credit_purchase",
)
def create_credit_purchase(
    request: CreditPurchaseRequest,
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> CreditPurchaseResponse:
    return CreditPurchaseService(services.context.database, services.payment_provider).create(
        user_id=user_id,
        request_key=str(request.request_key),
        amount_cents=request.amount_cents,
        kind=CreditPurchaseKind.Manual,
        success_url=_own_url(services, request.return_url),
        cancel_url=_own_url(services, request.cancel_url or request.return_url),
    )


@router.get(
    "/credit-purchases/{purchase_id}",
    response_model=CreditPurchaseResponse,
    operation_id="get_credit_purchase",
)
def get_credit_purchase(
    purchase_id: UUID,
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> CreditPurchaseResponse:
    return CreditPurchaseService(services.context.database, services.payment_provider).get(
        user_id=user_id, purchase_id=str(purchase_id)
    )


@router.get(
    "/summary",
    response_model=BillingSummaryResponse,
    operation_id="get_billing_summary",
)
def billing_summary(
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> BillingSummaryResponse:
    """What this account is on, and what it has left to spend.

    Answered about the signed-in person rather than a workspace: the provider
    invoices an account, and someone running dev, staging and prod holds three
    workspaces against one payment relationship.
    """

    with services.context.database.session() as session:
        standing = BillingStandingService(session).standing(user_id=user_id, at=utc_now())
    return _summary(standing)


@router.get(
    "/costs",
    response_model=UsageCostListResponse,
    operation_id="list_account_costs",
)
def account_costs(
    start: datetime,
    end: datetime,
    user_id: read_user,
    group_by: UsageCostGroupKey = UsageCostGroupKey.App,
    app_id: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_COST_PAGE),
    cursor: str | None = None,
    services: ApiServices = Depends(current_services),
) -> UsageCostListResponse:
    """What this account spent, across every workspace it is invoiced for.

    Beside the summary rather than under `/usage` for the reason the summary
    itself is: the provider invoices an account, so someone running dev, staging
    and prod wants one figure covering the three, and reaching it a workspace at
    a time leaves them adding up their own bill.

    Scoped to the rows this account is invoiced for, which is what the ledger's
    `owner_user_id` records and what the allowance and the invoice are summed
    over. Resolving the same scope through workspace membership would answer
    "what do I owe" with the spend of every workspace somebody added this person
    to, and leave off the workspaces they pay for but no longer belong to.

    `app_id` narrows to one app, which is how a caller reads what the workloads
    inside it cost without a second scope to authorize: the payer still decides
    which rows are summed, so an id belonging to somebody else's app selects
    rows this account has none of and totals nothing. An empty value is a filter
    rather than an absent one, and selects the usage that reached no app at all.
    """

    with services.context.database.session() as session:
        page = UsageCostService(session).costs(
            scope=PayerCostScope(user_id),
            start=start,
            end=end,
            group_by=group_by,
            app_id=app_id,
            limit=limit,
            cursor=cursor,
        )
    # No workspace named: this page covers an account, and picking one of its
    # workspaces to label it with would state a scope the page does not have.
    return usage_cost_list_response(
        page,
        workspace_id="",
        start=start,
        end=end,
        group_by=group_by,
    )


@router.get(
    "/cost-series",
    response_model=UsageCostSeriesResponse,
    operation_id="get_account_cost_series",
)
def account_cost_series(
    start: datetime,
    end: datetime,
    user_id: read_user,
    bucket: UsageCostBucket = UsageCostBucket.Day,
    services: ApiServices = Depends(current_services),
) -> UsageCostSeriesResponse:
    """What this account spent over a window, interval by interval.

    Beside the total rather than derived from it: a bill is one figure, and the
    question a customer asks next is which day it came from. One request answers
    the whole chart — a request per bar would be thirty scans of the same index,
    and two of them reading either side of a metering write would draw a shape
    the total does not add up to.

    Scoped to what this account is invoiced for, the same way the total beside
    it is and for the same reason: the two are read off one page, so a shape
    drawn over a different set of rows from the figure above it is a chart that
    does not add up to its own total.
    """

    with services.context.database.session() as session:
        series = UsageCostService(session).series(
            scope=PayerCostScope(user_id),
            start=start,
            end=end,
            bucket=bucket,
        )
    return _series_response(series)


@router.post(
    "/subscription",
    response_model=BillingSummaryResponse,
    operation_id="change_billing_plan",
)
def change_billing_plan(
    request: BillingPlanChangeRequest,
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> BillingSummaryResponse:
    """Move this account onto a published plan, with the usage that plan includes.

    A price changed on the subscription the account already holds rather than a
    subscription created — and, moving down, never a subscription cancelled: the
    billing anniversary, the three metered items and the usage already metered
    this cycle all survive it. `200` rather than `201` for that reason, and no
    `DELETE` beside it — every account already holds the subscription this
    modifies, nothing here has a new address for a caller to follow, and a verb
    saying the subscription is deleted would be the contract stating the one
    thing this design exists not to do.

    The body names a plan id from the closed set the platform publishes and
    never a price: the caller chose from the card the platform generated, and the
    server resolves what that plan costs from the same card. Moving onto dearer
    terms charges the prorated difference at once; moving onto cheaper ones
    charges and refunds nothing, leaves the cycle on the allowance it opened
    with, and applies from the next invoice.

    Answers with the standing the account now has rather than the provider's
    record of the subscription. What the caller does next is decided by what they
    may spend and until when, and the subscription's own identifiers are the
    provider's business — publishing them here would put a second name for the
    same relationship on the wire.

    `409` where a change for this account is already being settled: the provider
    charges the proration inside the call, so letting a second one through would
    be a customer charged twice for one upgrade.
    """

    BillingPlanChangeService(
        database=services.context.database,
        payments=services.payment_provider,
        events=services.events,
    ).change_plan(user_id=user_id, target=request.plan)
    with services.context.database.session() as session:
        return _summary(BillingStandingService(session).standing(user_id=user_id, at=utc_now()))


@router.post(
    "/card-session",
    response_model=BillingHostedSessionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="start_billing_card_setup",
)
def start_billing_card_setup(
    request: BillingHostedSessionRequest,
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> BillingHostedSessionResponse:
    """Begin saving a card, at the payment provider.

    The card is collected on the provider's own page: this returns somewhere to
    send the person, and the only thing that comes back to this platform is that
    a payment method now exists. Nothing here ever sees a card number, which is
    what keeps this repository outside the scope of cardholder data rules.

    The account is provisioned here too, because the page cannot be opened
    without a customer. Anyone who signed in was provisioned then, so this reads
    what already exists; an account reaching this page is one this platform will
    bill, so an administrator-minted one gets its customer, its subscription and
    its allowance here for the same reason sign-in gives them.
    """

    # Checked before anything else happens. A rejected address must not leave a
    # customer registered at the provider on the strength of a request that was
    # never going to be served.
    return_url = _own_url(services, request.return_url)
    cancel_url = _own_url(services, request.cancel_url or request.return_url)
    provider = services.payment_provider()
    with services.context.database.session() as session:
        account = BillingAccountService(session).billing_account_for(
            provider,
            user_id=user_id,
            workspace_id=owned_workspace_id(session, user_id),
        )
        session.commit()
    session_url = provider.card_setup_session(
        provider_customer_id=account.provider_customer_id,
        # Required by the provider even though the page charges nothing, and it
        # has to be the currency the customer is later charged in.
        currency=BILLING_CURRENCY,
        success_url=return_url,
        cancel_url=cancel_url,
    )
    return BillingHostedSessionResponse(url=session_url.url)


@router.post(
    "/portal-session",
    response_model=BillingHostedSessionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="start_billing_portal",
)
def start_billing_portal(
    request: BillingHostedSessionRequest,
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> BillingHostedSessionResponse:
    """Open the provider's page for managing what this platform bills.

    Replacing a card and reading past invoices. Refused for an account named
    nowhere at the provider — one that has never signed in — because there is
    nothing there to manage and the provider has nobody to show; such a caller
    wants the card page instead.
    """

    return_url = _own_url(services, request.return_url)
    with services.context.database.session() as session:
        account = BillingAccountService(session).payment_account_for(user_id=user_id)
    session_url = services.payment_provider().customer_portal_session(
        provider_customer_id=account.provider_customer_id,
        return_url=return_url,
    )
    return BillingHostedSessionResponse(url=session_url.url)


def _series_response(series: UsageCostSeries) -> UsageCostSeriesResponse:
    return UsageCostSeriesResponse(
        start=series.start,
        end=series.end,
        currency=BILLING_CURRENCY,
        bucket=series.bucket,
        cost_nanos=series.cost_nanos,
        data=[
            UsageCostBucketResponse(
                started_at=interval.started_at,
                ended_at=interval.ended_at,
                cost_nanos=interval.cost_nanos,
                dimensions=[
                    UsageCostDimensionTotalResponse(
                        dimension=total.dimension,
                        cost_nanos=total.cost_nanos,
                    )
                    for total in interval.dimensions
                ],
            )
            for interval in series.intervals
        ],
    )


def _summary(standing: BillingStanding) -> BillingSummaryResponse:
    allowance = standing.allowance
    return BillingSummaryResponse(
        status=standing.status,
        currency=BILLING_CURRENCY,
        plan=(
            BillingPlanResponse(
                id=standing.plan,
                name=published_plan(standing.plan).name,
                allowance=(
                    BillingAllowanceResponse(
                        period_started_at=allowance.started_at,
                        period_ended_at=allowance.ended_at,
                        allowance_nanos=allowance.allowance_nanos,
                        spent_nanos=allowance.spent_nanos,
                        remaining_nanos=allowance.remaining_nanos,
                    )
                    if allowance is not None
                    else None
                ),
            )
            if standing.plan is not None
            else None
        ),
        portal_available=standing.portal_available,
        payment_method_on_file=standing.payment_method_on_file,
        entitlements=(
            PlanEntitlementsResponse(
                max_concurrent_cpu_containers=(standing.entitlements.max_concurrent_cpu_containers),
                max_concurrent_gpus=standing.entitlements.max_concurrent_gpus,
                gpu_types=(
                    "all"
                    if standing.entitlements.gpu_types == "all"
                    else list(standing.entitlements.allowed_gpu_types)
                ),
                max_workspaces=standing.entitlements.max_workspaces,
                max_members=standing.entitlements.max_members,
                connected_cloud=standing.entitlements.connected_cloud,
                custom_domains=standing.entitlements.custom_domains,
                self_hosted=standing.entitlements.self_hosted,
                log_retention_days=standing.entitlements.log_retention_days,
                region_selection=standing.entitlements.region_selection,
            )
            if standing.entitlements is not None
            else None
        ),
        usage=BillingEntitlementUsageResponse(
            concurrent_cpu_containers=standing.usage.concurrent_cpu_containers,
            concurrent_gpus=standing.usage.concurrent_gpus,
            workspaces=standing.usage.workspaces,
            members=standing.usage.members,
            connected_clouds=standing.usage.connected_clouds,
            custom_domains=standing.usage.custom_domains,
        ),
        complimentary_since=standing.complimentary_since,
        plan_change_pending=standing.plan_change_pending,
    )


@router.get(
    "/accounts",
    response_model=BillingAccountAdminListResponse,
    operation_id="list_billing_accounts",
)
def list_billing_accounts(
    _auth: admin_access,
    limit: int = Query(50, ge=1, le=MAX_ACCOUNT_PAGE),
    cursor: str | None = None,
    search: str = Query("", max_length=200),
    role: PlatformRole | None = None,
    status: UserStatus | None = None,
    services: ApiServices = Depends(current_services),
) -> BillingAccountAdminListResponse:
    """Every account on the platform with its standing and recent spend.

    Walked by user rather than by billing row, so an account that has not
    signed in yet is listed beside the ones that have.

    `search` matches a display name, an email address, or the GitHub login,
    because those are the three things an operator has when somebody asks them
    to look an account up. Narrowing happens in the query rather than in the
    browser: the list pages, so filtering what arrived would hide every match
    that had not been fetched yet.
    """

    with services.context.database.session() as session:
        page = BillingAccountAdminService(session).list(
            after_user_id=decode_account_cursor(cursor),
            limit=limit,
            at=utc_now(),
            search=search,
            role=role,
            status=status,
        )
    identities = services.users.identities([row.user.id for row in page.rows])
    return BillingAccountAdminListResponse(
        data=[
            _administered_account(row, identities.get(row.user.id), since=page.recent_cost_since)
            for row in page.rows
        ],
        next=page.next,
    )


@router.put(
    "/accounts/{user_id}/complimentary",
    response_model=BillingAccountAdminResponse,
    operation_id="set_billing_complimentary",
)
def set_billing_complimentary(
    user_id: str,
    request: BillingComplimentaryRequest,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> BillingAccountAdminResponse:
    """Waive an account's bill, or stop waiving it. An administrator's decision alone."""

    now = utc_now()
    with services.context.database.session() as session:
        row = BillingAccountAdminService(session).set_complimentary(
            services.events,
            user_id=user_id,
            present=request.complimentary,
            at=now,
        )
        session.commit()
    return _administered_account(
        row, services.users.identity(user_id), since=now - RECENT_COST_WINDOW
    )


def _administered_account(
    row: AdministeredAccount,
    identity: UserIdentityRecord | None,
    *,
    since: datetime,
) -> BillingAccountAdminResponse:
    account = row.account
    return BillingAccountAdminResponse(
        user=user_response(row.user, identity),
        status=account.status if account is not None else None,
        plan=account.plan if account is not None else None,
        payment_method_on_file=(
            account is not None and account.payment_method_attached_at is not None
        ),
        complimentary_since=account.complimentary_since if account is not None else None,
        recent_cost_nanos=row.recent_cost_nanos,
        recent_cost_since=since,
    )


def _own_url(services: ApiServices, candidate: str) -> str:
    """Refuse to send anyone anywhere but back here.

    The provider redirects the person to whatever this hands it, so an
    unchecked value is an open redirect on the page that follows saving a card —
    which is the one worth abusing, because the person arriving at it is
    expecting to be somewhere unfamiliar and to have just done something with
    their money.
    """

    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        # `urlparse` refuses a host that NFKC-normalizes into a delimiter, which
        # is a hostile address rather than an unexpected one — it must end as the
        # refusal this route promises, not as an unhandled error.
        raise InvalidInputError("a billing return address must be a valid URL") from exc
    expected = urlparse(services.gateway_settings.public_http_url)
    if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
        raise InvalidInputError("a billing return address must be on this platform")
    return candidate
