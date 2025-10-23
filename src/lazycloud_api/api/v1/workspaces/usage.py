import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import get_workspace_with_admin_access
from lazycloud_api.database import db
from lazycloud_api.database.user_workspaces import UserWorkspacePydantic
from lazycloud_api.services import metrics_service
from lazycloud_api.services.k8s import create_ns_name
from shared.responses.usage import (
    CurrentUsageData,
    CurrentUsageResponse,
    ServiceBreakdownItem,
    UsageMetrics,
    UsagePeriodInfo,
    WorkspaceUsageBreakdownResponse,
    WorkspaceUsageResponse,
)

usage_router = APIRouter(prefix="/usage")


@usage_router.get("")
async def get_workspace_usage(
    workspace: UserWorkspacePydantic = Depends(get_workspace_with_admin_access),
    start_date: datetime = Query(..., description="Start date for usage query"),
    end_date: datetime = Query(..., description="End date for usage query"),
) -> WorkspaceUsageResponse:
    return WorkspaceUsageResponse(
        workspace_id=workspace.workspace_id,
        period=UsagePeriodInfo(start=start_date, end=end_date),
        usage=UsageMetrics(
            cpu_core_hours=0.0,
            memory_gb_hours=0.0,
            storage_gb_hours=0.0,
        ),
        record_count=0,
    )


@usage_router.get("/current")
async def get_current_usage(
    workspace: UserWorkspacePydantic = Depends(get_workspace_with_admin_access),
) -> CurrentUsageResponse:
    namespace = create_ns_name(workspace.workspace_id)

    try:
        summary = await metrics_service.get_namespace_summary(namespace)

        return CurrentUsageResponse(
            workspace_id=workspace.workspace_id,
            namespace=namespace,
            timestamp=summary.timestamp,
            current_usage=CurrentUsageData(
                cpu_cores=summary.cpu_cores,
                memory_gb=summary.memory_gb,
                storage_gb=summary.storage_gb,
            ),
        )

    except Exception as e:
        logger.error(
            f"Error getting current usage for workspace {workspace.workspace_id}: {e}"
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch current usage: {str(e)}"
        )


@usage_router.get("/breakdown")
async def get_workspace_usage_breakdown(
    workspace: UserWorkspacePydantic = Depends(get_workspace_with_admin_access),
    start_date: datetime = Query(..., description="Start date for usage query"),
    end_date: datetime = Query(..., description="End date for usage query"),
) -> WorkspaceUsageBreakdownResponse:
    try:
        records = await db.usage.get_workspace_usage(
            workspace_id=uuid.UUID(workspace.workspace_id),
            start_date=start_date,
            end_date=end_date,
        )

        # Aggregate breakdowns by service from all records
        by_service: dict[str, ServiceBreakdownItem] = {}

        for record in records:
            # Breakdowns are always present (loaded via joinedload, may be empty list)
            for breakdown in record.breakdowns:
                service_name = breakdown.service_name

                if service_name not in by_service:
                    by_service[service_name] = ServiceBreakdownItem(
                        cpu_core_hours=0.0,
                        memory_gb_hours=0.0,
                        pod_count=0,
                    )

                # Aggregate usage (convert seconds to hours)
                by_service[service_name].cpu_core_hours += (
                    breakdown.cpu_core_seconds / 3600
                )
                by_service[service_name].memory_gb_hours += (
                    breakdown.memory_gb_seconds / 3600
                )
                # Use max pod count across all records for this service
                by_service[service_name].pod_count = max(
                    by_service[service_name].pod_count, breakdown.pod_count
                )

        total_cpu = sum(r.cpu_core_seconds for r in records)
        total_memory = sum(r.memory_gb_seconds for r in records)
        total_storage = sum(r.storage_gb_hours for r in records)

        return WorkspaceUsageBreakdownResponse(
            workspace_id=workspace.workspace_id,
            period=UsagePeriodInfo(start=start_date, end=end_date),
            usage=UsageMetrics(
                cpu_core_hours=total_cpu / 3600,
                memory_gb_hours=total_memory / 3600,
                storage_gb_hours=total_storage,
            ),
            by_service=by_service,
        )

    except Exception as e:
        logger.error(f"Error getting usage breakdown for {workspace.workspace_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch usage breakdown: {str(e)}"
        )
