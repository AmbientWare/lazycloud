from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from observability.billing import UsageBillingReport, usage_billing_report_csv
from shared.http.usage import (
    UsageAggregationResponse,
    UsageBillingAppSummaryResponse,
    UsageBillingAttributionResponse,
    UsageBillingBucketResponse,
    UsageBillingLineResponse,
    UsageBillingOverviewResponse,
    UsageBillingPeriod,
    UsageBillingWorkloadListResponse,
    UsageRecordListResponse,
    UsageRecordResponse,
    UsageSummaryResponse,
)
from shared.usage import UsageGroupKey, UsageMetric
from shared.usage_query import UsageQuery

from api.server.auth import read_workspace
from api.server.dependencies import current_services
from api.server.routers.resource_api.common import _parsed_time
from api.server.services import ApiServices

router = APIRouter()


@router.get(
    "/api/v1/usage/billing.csv",
    response_class=Response,
    operation_id="export_usage_billing_report_csv",
)
def usage_billing_report_csv_export(
    start: datetime,
    end: datetime,
    bucket_seconds: int = Query(3600, ge=300, le=86_400),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> Response:
    report = _full_billing_report(
        workspace_id=workspace_id,
        start=start,
        end=end,
        bucket_seconds=bucket_seconds,
        services=services,
    )
    filename = f"usage-{report.start.date().isoformat()}-to-{report.end.date().isoformat()}.csv"
    return Response(
        content=usage_billing_report_csv(report),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/api/v1/usage/billing",
    response_model=UsageBillingOverviewResponse,
    operation_id="get_usage_billing_overview",
)
def usage_billing_overview(
    start: datetime | None = None,
    end: datetime | None = None,
    period: UsageBillingPeriod | None = None,
    bucket_seconds: int = Query(3600, ge=300, le=86_400),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> UsageBillingOverviewResponse:
    report = services.usage.billing_overview(
        workspace_id=workspace_id,
        start=start,
        end=end,
        period=period,
        bucket_seconds=bucket_seconds,
    )
    return UsageBillingOverviewResponse(
        workspace_id=report.workspace_id,
        start=report.start,
        end=report.end,
        currency=report.currency,
        total_cost_nanos=report.total_cost_nanos,
        contains_estimates=report.contains_estimates,
        summary=[UsageBillingLineResponse.model_validate(item) for item in report.summary],
        apps=[UsageBillingAppSummaryResponse.model_validate(item) for item in report.apps],
        activity=[UsageBillingBucketResponse.model_validate(item) for item in report.activity],
    )


@router.get(
    "/api/v1/usage/billing/workloads",
    response_model=UsageBillingWorkloadListResponse,
    operation_id="list_usage_billing_workloads",
)
def usage_billing_workloads(
    start: datetime,
    end: datetime,
    app_id: str = Query(),
    bucket_seconds: int = Query(3600, ge=300, le=86_400),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> UsageBillingWorkloadListResponse:
    report = services.usage.billing_workloads(
        workspace_id=workspace_id,
        app_id=app_id,
        start=start,
        end=end,
        bucket_seconds=bucket_seconds,
    )
    return UsageBillingWorkloadListResponse(
        workspace_id=report.workspace_id,
        app_id=report.app_id,
        start=report.start,
        end=report.end,
        currency=report.currency,
        data=[UsageBillingAttributionResponse.model_validate(item) for item in report.data],
    )


def _full_billing_report(
    *,
    workspace_id: str,
    start: datetime,
    end: datetime,
    bucket_seconds: int,
    services: ApiServices,
) -> UsageBillingReport:
    return services.usage.billing_report(
        workspace_id=workspace_id,
        start=start,
        end=end,
        bucket_seconds=bucket_seconds,
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
