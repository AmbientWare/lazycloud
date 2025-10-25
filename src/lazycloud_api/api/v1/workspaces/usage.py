from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import get_workspace_with_admin_access
from lazycloud_api.database.user_workspaces import UserWorkspacePydantic
from lazycloud_api.services import PrometheusMetricsService, get_metrics_service
from lazycloud_api.services.k8s import create_ns_name
from shared.responses.usage import (
    CurrentUsageData,
    CurrentUsageResponse,
    UsageMetrics,
    UsagePeriodInfo,
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
    metrics_service: PrometheusMetricsService = Depends(get_metrics_service),
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
