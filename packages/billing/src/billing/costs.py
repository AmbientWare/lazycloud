from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import (
    BillingAllowanceRepository,
    SpentAllowancePeriod,
)
from database.repositories.billing_costs import (
    BillingLedgerCostRepository,
    LedgerCostCursor,
    LedgerCostRow,
    LedgerCostScope,
)
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_plan_changes import BillingPlanChangeIntentRepository
from database.repositories.compute import AwsAccountConnectionRepository
from database.repositories.custom_domains import CustomDomainRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.billing_quotes import BilledDimension
from shared.billing_rate_card import PlanEntitlements, account_terms, complimentary_terms
from shared.contracts import ContractModel
from shared.errors import InvalidInputError
from shared.funding import FundingBalance
from shared.http.usage import UsageCostBucket, UsageCostGroupKey
from shared.timestamps import to_utc
from sqlalchemy.orm import Session

from billing.funding import BillingFundingService

MAX_COST_PAGE = 200
MAX_COST_WINDOW_DAYS = 400
"""The longest window a single request may total.

A cost window is scanned over an index leading with the scope it was asked at —
`(workspace_id, segment_started_at)` or `(owner_user_id, segment_started_at)` —
so an unbounded one is a full-table read somebody can ask for by editing a URL.
Just over a year, which covers every period anybody has a reason to look at.
"""

MAX_COST_INTERVALS = 400
"""The most intervals one series may be cut into.

The window cap alone does not bound this: hourly intervals over the same year
would be nine thousand of them, which is a response nobody can read and a chart
nobody can draw. Matched to the window cap so a year of days is exactly reachable
and an hourly request has to name a fortnight.
"""

_BUCKET_WIDTHS: dict[UsageCostBucket, timedelta] = {
    UsageCostBucket.Hour: timedelta(hours=1),
    UsageCostBucket.Day: timedelta(days=1),
}


@dataclass(frozen=True, slots=True)
class BillingEntitlementUsage:
    """Account-wide usage measured against plan entitlements.

    Concurrency is two figures because the plan bounds two pools: containers
    holding no card, and the cards themselves. Reported as one number they would
    be read against whichever limit the surface happened to draw them beside.
    """

    concurrent_cpu_containers: int
    concurrent_gpus: int
    workspaces: int
    members: int
    connected_clouds: int
    custom_domains: int


@dataclass(frozen=True, slots=True)
class BillingCreditSummary:
    compute: FundingBalance
    storage_and_transfer: FundingBalance
    ready: bool


@dataclass(frozen=True, slots=True)
class BillingStanding:
    """What an account is on and where it stands.

    `plan` and `allowance` are absent together: the period was opened from the
    subscription's own cycle and stamped with what that plan includes, so an
    account on no plan has nothing to report against either. `portal_available`
    is a separate fact from both — it says whether the provider has a page to
    show, which is true from the moment a customer exists.
    """

    status: BillingAccountStatus
    plan: BillingPlanId | None
    portal_available: bool
    allowance: SpentAllowancePeriod | None
    payment_method_on_file: bool
    """Whether anybody can be charged for what this account spends next.

    Distinct from `portal_available`, which is true from the moment a customer
    record exists and says only that the provider has a page to show. This is
    what decides how much the account is given and whether running work is
    stopped when that runs out, so the dashboard has to be able to tell a person
    which of the two states they are in.
    """

    entitlements: PlanEntitlements | None
    """What the current plan grants, absent when the account has no plan."""

    usage: BillingEntitlementUsage
    """What the account currently consumes across every workspace it owns."""

    complimentary_since: datetime | None
    """When an administrator waived this account's bill, `None` while nobody has.

    Reported beside `plan` rather than in place of it. The subscription is
    still what the account holds and what it returns to when the waiver is
    withdrawn; the entitlements beside it are the waiver's, since those are
    what admission is actually holding the account to.
    """

    plan_change_pending: bool
    """Whether a change of plan for this account is still being settled.

    The plan beside it is what the account holds now, which is the answer to a
    different question from the one somebody who has just pressed a button is
    asking. A change whose outcome nobody could establish is retried for hours,
    and without this the surface shows the old plan next to a live button that
    answers a conflict — an invisible pending change, which is the state the
    intent row exists to make visible rather than to hide.
    """


@dataclass(frozen=True, slots=True)
class UsageCostPage:
    """One page of rows, and what the whole window they came from cost.

    `cost_nanos` totals the window rather than the page, so the figure a customer
    reads as their bill never depends on how far they scrolled.
    """

    cost_nanos: int
    rows: tuple[LedgerCostRow, ...]
    next: str


@dataclass(frozen=True, slots=True)
class UsageCostDimensionTotal:
    """One invoice line's share of an interval."""

    dimension: BilledDimension
    cost_nanos: int


@dataclass(frozen=True, slots=True)
class UsageCostInterval:
    """What one interval of a window cost, and what it was charged under.

    `ended_at` is clipped to the end of the window, so the last interval of a
    period still running reports the span it actually covers.
    """

    started_at: datetime
    ended_at: datetime
    cost_nanos: int
    dimensions: tuple[UsageCostDimensionTotal, ...]


@dataclass(frozen=True, slots=True)
class UsageCostSeries:
    """A window's spend, in the shape it took over time.

    Every interval the window covers is present, quiet ones included: a chart
    that skips them draws a fortnight of nothing as a fortnight of something.
    `cost_nanos` is those intervals summed rather than a second total read
    separately, so the figure and the bars it is drawn from cannot disagree.
    """

    bucket: UsageCostBucket
    start: datetime
    end: datetime
    """The window as it was measured, in UTC.

    Echoed rather than left to the caller because a request may name its window
    without an offset, and a naive value read as local time here and as UTC by
    the database would place the bars beside the wrong hours.
    """

    cost_nanos: int
    intervals: tuple[UsageCostInterval, ...]


class _CostCursorPayload(ContractModel):
    cost_nanos: int
    group_key: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BillingStandingService:
    """What to tell a customer about their own payment relationship.

    Reads the account and the allowance period together so the two describe the
    same instant. An account with no row has never reached a billing surface, so
    it is named nowhere at the provider, is on no plan, and holds no period —
    only an administrator can mint one, and showing it terms nothing will hold it
    to would be inventing them.
    """

    session: Session

    def credit_balance(self, *, user_id: str, at: datetime) -> BillingCreditSummary:
        BillingAccountRepository(self.session).get_by_user(user_id, for_update=True)
        cutover = BillingCreditRepository(self.session).cutover(user_id=user_id)
        funding = BillingFundingService(self.session)
        return BillingCreditSummary(
            compute=funding.balance(
                user_id=user_id, dimension=BilledDimension.ComputeRuntime, at=at
            ),
            storage_and_transfer=funding.balance(
                user_id=user_id, dimension=BilledDimension.NetworkEgress, at=at
            ),
            ready=cutover is not None and cutover.completed_at is not None,
        )

    def standing(self, *, user_id: str, at: datetime) -> BillingStanding:
        usage = self._entitlement_usage(user_id=user_id)
        account = BillingAccountRepository(self.session).get_by_user(user_id)
        if account is None:
            return BillingStanding(
                status=BillingAccountStatus.Active,
                plan=None,
                portal_available=False,
                allowance=None,
                payment_method_on_file=False,
                entitlements=None,
                usage=usage,
                complimentary_since=None,
                plan_change_pending=False,
            )
        has_card = account.payment_method_attached_at is not None
        # An account on no plan is shown no ceiling rather than a default one:
        # it may run nothing at all until it is provisioned, and a number here
        # would read as headroom it does not have. A waived account is the
        # exception, and is shown the ceiling admission holds it to.
        if account.complimentary_since is not None:
            terms = complimentary_terms()
        elif account.plan is not None:
            terms = account_terms(account.plan, has_payment_method=has_card)
        else:
            terms = None
        return BillingStanding(
            status=account.status,
            plan=account.plan,
            portal_available=bool(account.provider_customer_id),
            allowance=BillingAllowanceRepository(self.session).current_period(
                user_id=user_id,
                at=at,
            ),
            payment_method_on_file=has_card,
            entitlements=terms.entitlements if terms else None,
            usage=usage,
            complimentary_since=account.complimentary_since,
            plan_change_pending=BillingPlanChangeIntentRepository(self.session).has_open(
                user_id=user_id
            ),
        )

    def _entitlement_usage(self, *, user_id: str) -> BillingEntitlementUsage:
        connection = AwsAccountConnectionRepository(self.session).get_for_user(user_id)
        containers = ContainerRepository(self.session)
        members = WorkspaceMemberRepository(self.session)
        return BillingEntitlementUsage(
            concurrent_cpu_containers=containers.count_live_cpu_for_owner(owner_user_id=user_id),
            concurrent_gpus=containers.count_live_gpus_for_owner(owner_user_id=user_id),
            workspaces=members.owned_workspace_count(user_id),
            members=members.distinct_member_count_for_owner(user_id),
            connected_clouds=int(connection is not None and not connection.platform_fleet),
            custom_domains=CustomDomainRepository(self.session).count_for_user(user_id),
        )


@dataclass(frozen=True, slots=True)
class UsageCostService:
    """What usage cost, attributed to what ran it.

    Reads the priced ledger rather than recomputing anything from usage: the
    segments already carry the frozen cost, the quantity and the components, so
    the dashboard and the invoice are summing the same rows.

    Takes a scope rather than a workspace, because the same question is asked at
    two levels: a workspace looking at what was spent in it, and an account
    looking at what it is invoiced for. One query answers both, so the total on
    the billing page and the total on a workspace page cannot drift into two
    calculations that have to agree.
    """

    session: Session

    def costs(
        self,
        *,
        scope: LedgerCostScope,
        start: datetime,
        end: datetime,
        group_by: UsageCostGroupKey,
        limit: int,
        app_id: str | None = None,
        workload_id: str | None = None,
        cursor: str | None = None,
    ) -> UsageCostPage:
        """One page of what this scope spent, and what the whole window cost.

        Rows carry the workspace they were incurred in whatever the scope was, so
        an account page groups and labels by workspace without the scope having
        enumerated any.
        """

        _checked_window(start, end)
        if limit < 1 or limit > MAX_COST_PAGE:
            raise InvalidInputError(f"a cost page holds between 1 and {MAX_COST_PAGE} rows")
        repository = BillingLedgerCostRepository(self.session)
        page = repository.page(
            scope=scope,
            start=start,
            end=end,
            group_by=group_by,
            limit=limit,
            app_id=app_id,
            workload_id=workload_id,
            cursor=_decode_cursor(cursor),
        )
        return UsageCostPage(
            cost_nanos=repository.window_cost_nanos(
                scope=scope,
                start=start,
                end=end,
                app_id=app_id,
                workload_id=workload_id,
            ),
            rows=page.rows,
            next=_encode_cursor(page.next),
        )

    def series(
        self,
        *,
        scope: LedgerCostScope,
        start: datetime,
        end: datetime,
        bucket: UsageCostBucket,
    ) -> UsageCostSeries:
        """What the window cost, interval by interval.

        One read for the whole window rather than one per interval: a chart of a
        month is thirty-one questions with the same answer, and asking them
        separately is thirty-one scans of the same index and thirty-one chances
        for two of them to straddle a boundary.

        Intervals are whole `bucket` widths measured from `start`, so a caller
        that opens its window on a UTC boundary reads UTC days. Every interval
        the window covers is returned, including the ones nothing ran in.
        """

        start, end = to_utc(start), to_utc(end)
        _checked_window(start, end)
        width = _BUCKET_WIDTHS[bucket]
        count = -(-(end - start) // width)
        if count > MAX_COST_INTERVALS:
            raise InvalidInputError(
                f"a cost series holds at most {MAX_COST_INTERVALS} intervals; "
                f"this window is {count} of them"
            )
        totals: dict[int, list[UsageCostDimensionTotal]] = {}
        for row in BillingLedgerCostRepository(self.session).bucket_totals(
            scope=scope,
            start=start,
            end=end,
            width_seconds=int(width.total_seconds()),
        ):
            totals.setdefault(row.index, []).append(
                UsageCostDimensionTotal(dimension=row.dimension, cost_nanos=row.cost_nanos)
            )
        intervals = tuple(
            _interval(start=start, end=end, width=width, index=index, totals=totals.get(index, ()))
            for index in range(count)
        )
        return UsageCostSeries(
            bucket=bucket,
            start=start,
            end=end,
            cost_nanos=sum(interval.cost_nanos for interval in intervals),
            intervals=intervals,
        )


def _checked_window(start: datetime, end: datetime) -> None:
    if end <= start:
        raise InvalidInputError("a cost window must end after it starts")
    if (end - start).days > MAX_COST_WINDOW_DAYS:
        raise InvalidInputError(f"a cost window may span at most {MAX_COST_WINDOW_DAYS} days")


def _interval(
    *,
    start: datetime,
    end: datetime,
    width: timedelta,
    index: int,
    totals: Sequence[UsageCostDimensionTotal],
) -> UsageCostInterval:
    started_at = start + width * index
    return UsageCostInterval(
        started_at=started_at,
        ended_at=min(started_at + width, end),
        cost_nanos=sum(total.cost_nanos for total in totals),
        dimensions=tuple(totals),
    )


def _encode_cursor(cursor: LedgerCostCursor | None) -> str:
    if cursor is None:
        return ""
    payload = _CostCursorPayload(cost_nanos=cursor.cost_nanos, group_key=cursor.group_key)
    return base64.urlsafe_b64encode(payload.model_dump_json().encode()).decode()


def _decode_cursor(cursor: str | None) -> LedgerCostCursor | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = _CostCursorPayload.model_validate_json(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidInputError("invalid usage cost cursor") from exc
    return LedgerCostCursor(cost_nanos=payload.cost_nanos, group_key=payload.group_key)


__all__ = [
    "MAX_COST_INTERVALS",
    "MAX_COST_PAGE",
    "MAX_COST_WINDOW_DAYS",
    "BillingStanding",
    "BillingStandingService",
    "UsageCostDimensionTotal",
    "UsageCostInterval",
    "UsageCostPage",
    "UsageCostSeries",
    "UsageCostService",
]
