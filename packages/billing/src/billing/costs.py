from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import (
    BillingAllowanceRepository,
    SpentAllowancePeriod,
)
from database.repositories.billing_costs import (
    BillingLedgerCostRepository,
    LedgerCostCursor,
    LedgerCostRow,
)
from database.repositories.billing_plan_changes import BillingPlanChangeIntentRepository
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import account_terms
from shared.contracts import ContractModel
from shared.errors import InvalidInputError
from shared.http.usage import UsageCostGroupKey
from sqlalchemy.orm import Session

MAX_COST_PAGE = 200
MAX_COST_WINDOW_DAYS = 400
"""The longest window a single request may total.

A cost window is scanned over an index on `(workspace_id, segment_started_at)`,
so an unbounded one is a full-table read somebody can ask for by editing a URL.
Just over a year, which covers every period anybody has a reason to look at.
"""


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

    max_concurrent_containers: int
    """How much this account may have running at once, on its current terms."""

    live_container_count: int
    """How much it has running or queued right now, across every workspace it
    owns — the same figure the limit is compared against, so a customer reading
    both sees why they were refused rather than a ceiling and no position."""

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

    def standing(self, *, user_id: str, at: datetime) -> BillingStanding:
        account = BillingAccountRepository(self.session).get_by_user(user_id)
        if account is None:
            return BillingStanding(
                status=BillingAccountStatus.Active,
                plan=None,
                portal_available=False,
                allowance=None,
                payment_method_on_file=False,
                max_concurrent_containers=0,
                live_container_count=0,
                plan_change_pending=False,
            )
        has_card = account.payment_method_attached_at is not None
        # An account on no plan is shown no ceiling rather than a default one:
        # it may run nothing at all until it is provisioned, and a number here
        # would read as headroom it does not have.
        terms = (
            account_terms(account.plan, has_payment_method=has_card)
            if account.plan is not None
            else None
        )
        return BillingStanding(
            status=account.status,
            plan=account.plan,
            portal_available=bool(account.provider_customer_id),
            allowance=BillingAllowanceRepository(self.session).current_period(
                user_id=user_id,
                at=at,
            ),
            payment_method_on_file=has_card,
            max_concurrent_containers=terms.max_concurrent_containers if terms else 0,
            live_container_count=ContainerRepository(self.session).count_live_for_owner(
                owner_user_id=user_id
            ),
            plan_change_pending=BillingPlanChangeIntentRepository(self.session).has_open(
                user_id=user_id
            ),
        )


@dataclass(frozen=True, slots=True)
class UsageCostService:
    """What a workspace's usage cost, attributed to what ran it.

    Reads the priced ledger rather than recomputing anything from usage: the
    segments already carry the frozen cost, the quantity and the components, so
    the dashboard and the invoice are summing the same rows.
    """

    session: Session

    def costs(
        self,
        *,
        workspace_id: str,
        start: datetime,
        end: datetime,
        group_by: UsageCostGroupKey,
        limit: int,
        app_id: str | None = None,
        workload_id: str | None = None,
        cursor: str | None = None,
    ) -> UsageCostPage:
        if end <= start:
            raise InvalidInputError("a cost window must end after it starts")
        if (end - start).days > MAX_COST_WINDOW_DAYS:
            raise InvalidInputError(f"a cost window may span at most {MAX_COST_WINDOW_DAYS} days")
        if limit < 1 or limit > MAX_COST_PAGE:
            raise InvalidInputError(f"a cost page holds between 1 and {MAX_COST_PAGE} rows")
        repository = BillingLedgerCostRepository(self.session)
        page = repository.page(
            workspace_id=workspace_id,
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
                workspace_id=workspace_id,
                start=start,
                end=end,
                app_id=app_id,
                workload_id=workload_id,
            ),
            rows=page.rows,
            next=_encode_cursor(page.next),
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
    "MAX_COST_PAGE",
    "MAX_COST_WINDOW_DAYS",
    "BillingStanding",
    "BillingStandingService",
    "UsageCostPage",
    "UsageCostService",
]
