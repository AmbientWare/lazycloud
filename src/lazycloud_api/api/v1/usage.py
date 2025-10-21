import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import require_workspace_member
from lazycloud_api.database import db
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

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/workspaces/{workspace_id}/current")
async def get_current_usage(
    workspace_id: str,
    _: None = Depends(require_workspace_member),
) -> CurrentUsageResponse:
    namespace = create_ns_name(workspace_id)

    try:
        summary = await metrics_service.get_namespace_summary(namespace)

        return CurrentUsageResponse(
            workspace_id=workspace_id,
            namespace=namespace,
            timestamp=summary.timestamp,
            current_usage=CurrentUsageData(
                cpu_cores=summary.cpu_cores,
                memory_gb=summary.memory_gb,
                storage_gb=summary.storage_gb,
            ),
        )

    except Exception as e:
        logger.error(f"Error getting current usage for workspace {workspace_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch current usage: {str(e)}"
        )


@router.get("/workspaces/{workspace_id}")
async def get_workspace_usage(
    workspace_id: str,
    start_date: datetime = Query(..., description="Start date for usage query"),
    end_date: datetime = Query(..., description="End date for usage query"),
    _: None = Depends(require_workspace_member),
) -> WorkspaceUsageResponse:
    try:
        records = await db.usage.get_workspace_usage(
            workspace_id=uuid.UUID(workspace_id),
            start_date=start_date,
            end_date=end_date,
        )

        total_cpu = sum(r.cpu_core_seconds for r in records)
        total_memory = sum(r.memory_gb_seconds for r in records)
        total_storage = sum(r.storage_gb_hours for r in records)

        return WorkspaceUsageResponse(
            workspace_id=workspace_id,
            period=UsagePeriodInfo(start=start_date, end=end_date),
            usage=UsageMetrics(
                cpu_core_hours=total_cpu / 3600,
                memory_gb_hours=total_memory / 3600,
                storage_gb_hours=total_storage,
            ),
            record_count=len(records),
        )

    except Exception as e:
        logger.error(f"Error getting workspace usage for {workspace_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch usage: {str(e)}")


@router.get("/workspaces/{workspace_id}/breakdown")
async def get_workspace_usage_breakdown(
    workspace_id: str,
    start_date: datetime = Query(..., description="Start date for usage query"),
    end_date: datetime = Query(..., description="End date for usage query"),
    _: None = Depends(require_workspace_member),
) -> WorkspaceUsageBreakdownResponse:
    try:
        records = await db.usage.get_workspace_usage(
            workspace_id=uuid.UUID(workspace_id),
            start_date=start_date,
            end_date=end_date,
        )

        # TODO: Implement service aggregation from breakdown records
        by_service: dict[str, ServiceBreakdownItem] = {}

        total_cpu = sum(r.cpu_core_seconds for r in records)
        total_memory = sum(r.memory_gb_seconds for r in records)
        total_storage = sum(r.storage_gb_hours for r in records)

        return WorkspaceUsageBreakdownResponse(
            workspace_id=workspace_id,
            period=UsagePeriodInfo(start=start_date, end=end_date),
            usage=UsageMetrics(
                cpu_core_hours=total_cpu / 3600,
                memory_gb_hours=total_memory / 3600,
                storage_gb_hours=total_storage,
            ),
            by_service=by_service,
        )

    except Exception as e:
        logger.error(f"Error getting usage breakdown for {workspace_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch usage breakdown: {str(e)}"
        )
