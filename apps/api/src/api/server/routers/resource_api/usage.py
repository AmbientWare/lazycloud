from __future__ import annotations

from datetime import datetime

from billing.costs import MAX_COST_PAGE, UsageCostService
from database.repositories.billing_costs import WorkspaceCostScope
from fastapi import APIRouter, Depends, Query
from shared.http.usage import (
    UsageAggregationResponse,
    UsageCostGroupKey,
    UsageCostListResponse,
    UsageRecordListResponse,
    UsageRecordResponse,
    UsageSummaryResponse,
)
from shared.usage import UsageGroupKey, UsageMetric
from shared.usage_query import UsageQuery

from api.server.auth import read_workspace
from api.server.dependencies import current_services
from api.server.routers.resource_api.common import _parsed_time, usage_cost_list_response
from api.server.services import ApiServices

router = APIRouter()


@router.get(
    "/api/v1/usage/costs",
    response_model=UsageCostListResponse,
    operation_id="list_usage_costs",
)
def usage_costs(
    start: datetime,
    end: datetime,
    group_by: UsageCostGroupKey = UsageCostGroupKey.App,
    app_id: str | None = None,
    workload_id: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_COST_PAGE),
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> UsageCostListResponse:
    """What this workspace's usage cost, attributed to what ran it.

    Read from the priced ledger, which is the same set of rows the provider is
    metered from — so the figure here and the figure on an invoice are one
    total summed twice rather than two calculations that have to agree.

    Scoped by where the cost arose and not by who is invoiced for it, which is
    the opposite of the account total under `/api/v1/billing`. A workspace costs
    what it costs to everyone who reaches it, and the token that reaches this
    names a workspace rather than a person — filtering it by payer would answer
    a member asking what this workspace spent with what they personally owe for
    it, which is nothing.
    """

    with services.context.database.session() as session:
        page = UsageCostService(session).costs(
            scope=WorkspaceCostScope((workspace_id,)),
            start=start,
            end=end,
            group_by=group_by,
            limit=limit,
            app_id=app_id,
            workload_id=workload_id,
            cursor=cursor,
        )
    return usage_cost_list_response(
        page,
        workspace_id=workspace_id,
        start=start,
        end=end,
        group_by=group_by,
    )


@router.get(
    "/api/v1/usage/records",
    response_model=UsageRecordListResponse,
    operation_id="list_usage_records",
)
def usage_records(
    metric: UsageMetric | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> UsageRecordListResponse:
    page = services.usage.list_page(
        _usage_query(
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            start=start,
            end=end,
        ),
        workspace_id=workspace_id,
        metric=metric,
        limit=limit,
        cursor=cursor,
    )
    return UsageRecordListResponse(
        data=[UsageRecordResponse.model_validate(item) for item in page.data],
        next=page.next,
    )


@router.get(
    "/api/v1/usage/summary",
    response_model=UsageSummaryResponse,
    operation_id="get_usage_summary",
)
def usage_summary(
    metric: UsageMetric | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    group_by: list[UsageGroupKey] = Query(default=[]),
    start: str | None = None,
    end: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> UsageSummaryResponse:
    rows = services.usage.aggregate(
        query=_usage_query(
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            start=start,
            end=end,
        ),
        metric=metric,
        group_by=tuple(group_by),
    )
    return UsageSummaryResponse(
        rows=[UsageAggregationResponse.model_validate(item) for item in rows]
    )


def _usage_query(
    *,
    workspace_id: str,
    resource_type: str | None,
    resource_id: str | None,
    start: str | None,
    end: str | None,
) -> UsageQuery:
    return UsageQuery(
        workspace_id=workspace_id,
        resource_type=resource_type,
        resource_id=resource_id,
        created_after=_parsed_time(start.replace("Z", "+00:00") if start else None),
        created_before=_parsed_time(end.replace("Z", "+00:00") if end else None),
    )
