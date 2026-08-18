from __future__ import annotations

from datetime import datetime

from billing.costs import UsageCostPage
from control.service import StubKind
from operations.management import ManagementService
from shared.errors import InvalidInputError
from shared.http.usage import (
    UsageCostComponentResponse,
    UsageCostGroupKey,
    UsageCostListResponse,
    UsageCostRowResponse,
)
from shared.payments import BILLING_CURRENCY

from api.server.services import ApiServices

STUB_TYPE_ALIASES = {
    "endpoint": StubKind.Endpoint,
    "http": StubKind.Endpoint,
    "asgi": StubKind.Asgi,
    "function": StubKind.Function,
    "pod": StubKind.Pod,
    "sandbox": StubKind.Sandbox,
}


def _management(services: ApiServices) -> ManagementService:
    return ManagementService(services)


def member_workspaces(services: ApiServices, user_id: str) -> dict[str, str]:
    """Every workspace this person reaches, and what each is called.

    The scope of an account-wide *reading*, resolved from the membership rows
    naming the workspaces rather than from anything a request supplied: a
    reading assembled from ids a caller named would be a reading of whatever it
    asked for. Ids are `list(mapping)`.

    Membership because the question these answer is what somebody is allowed to
    watch, which is not what they are billed for. An account's money is scoped
    by the payer on the ledger row instead, and the two scopes are deliberately
    different sets.

    Names come off the same read that decides the scope, because a row names the
    workspace it belongs to and looking those names up separately would be a
    second answer to which workspaces the answer covers.
    """

    return {workspace.id: workspace.name for workspace in services.users.workspaces(user_id)}


def _parsed_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"invalid timestamp: {value}"
        raise InvalidInputError(msg) from exc


def usage_cost_list_response(
    page: UsageCostPage,
    *,
    workspace_id: str,
    start: datetime,
    end: datetime,
    group_by: UsageCostGroupKey,
) -> UsageCostListResponse:
    """One page of priced ledger rows, as the wire shape.

    Shared because the same page is asked for at two scopes — one workspace, and
    one payer's whole account — and a second copy of this mapping is how one
    scope quietly stops carrying a field the other gained.
    """

    return UsageCostListResponse(
        workspace_id=workspace_id,
        start=start,
        end=end,
        currency=BILLING_CURRENCY,
        group_by=group_by,
        cost_nanos=page.cost_nanos,
        data=[
            UsageCostRowResponse(
                app_id=row.app_id,
                app_name=row.app_name,
                workspace_id=row.workspace_id,
                workspace_name=row.workspace_name,
                workload_id=row.workload_id,
                workload_name=row.workload_name,
                workload_kind=row.workload_kind,
                task_id=row.task_id,
                cost_nanos=row.cost_nanos,
                components=[
                    UsageCostComponentResponse(
                        dimension=total.dimension,
                        component=total.component,
                        quantity=float(total.quantity),
                        cost_nanos=total.cost_nanos,
                    )
                    for total in row.components
                ],
            )
            for row in page.rows
        ],
        next=page.next,
    )
