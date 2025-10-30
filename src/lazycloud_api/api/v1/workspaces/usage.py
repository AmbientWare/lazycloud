from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import get_workspace_with_admin_access
from lazycloud_api.database import db
from lazycloud_api.database.user_workspaces import UserWorkspacePydantic
from lazycloud_api.database.workspaces import WorkspacePydantic
from lazycloud_api.services import UsageService, get_usage_service
from shared.responses.usage import (
    DailyUsageData,
    DailyUsageResponse,
    UsageMetrics,
    UsagePeriodInfo,
    WorkspaceUsageResponse,
)

usage_router = APIRouter(prefix="/usage")


@usage_router.get("")
async def query_usage(
    workspace_membership: tuple[UserWorkspacePydantic, WorkspacePydantic] = Depends(
        get_workspace_with_admin_access
    ),
    start_date: datetime | None = Query(
        None, description="Start date (defaults to start of current month)"
    ),
    end_date: datetime | None = Query(None, description="End date (defaults to now)"),
    deployment_id: str | None = Query(
        None, description="Deployment ID to get detailed breakdown for"
    ),
    usage_service: UsageService = Depends(get_usage_service),
) -> WorkspaceUsageResponse:
    """Query workspace usage. When deployment_id provided, returns detailed service and volume breakdown."""
    _, workspace = workspace_membership

    try:
        # If deployment_id provided, return detailed breakdown
        if deployment_id:
            # Get deployment to verify it exists and belongs to workspace
            deployment = await db.compose_deployments.aget_by_id(deployment_id)
            if not deployment or deployment.workspace_id != workspace.id:
                raise HTTPException(status_code=404, detail="Deployment not found")

            # Get usage breakdown from service layer
            (
                metrics,
                services,
                volumes,
                usage_record,
            ) = await usage_service.get_workspace_usage_breakdown(
                workspace_id=workspace.id,
            )

            # Handle no data case
            if not usage_record:
                raise HTTPException(status_code=404, detail="Usage record not found")

            # Return detailed breakdown
            return WorkspaceUsageResponse(
                workspace_id=workspace.id,
                period=UsagePeriodInfo(
                    start=usage_record.collection_start,
                    end=usage_record.collection_end,
                ),
                usage=metrics,
                record_count=1,
                deployment_id=deployment_id,
                deployment_name=deployment.name,
                services=services,
                volumes=volumes,
            )

        # Otherwise, return workspace-level aggregation
        now = datetime.now(timezone.utc)
        if not start_date:
            logger.info("No start date provided, using default start of current month")
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            logger.info("No end date provided, using default end of current month")
            end_date = now

        # Aggregate usage records from database for the period
        usage_records = await db.usage.get_workspace_usage(
            workspace_id=workspace.id,
            start_date=start_date,
            end_date=end_date,
        )

        # Sum all usage from records
        total_cpu_seconds = sum(r.cpu_core_seconds for r in usage_records)
        total_memory_seconds = sum(r.memory_gb_seconds for r in usage_records)
        total_s3_hours = sum(r.s3_gb_hours for r in usage_records)
        total_efs_hours = sum(r.efs_gb_hours for r in usage_records)

        return WorkspaceUsageResponse(
            workspace_id=workspace.id,
            period=UsagePeriodInfo(start=start_date, end=end_date),
            usage=UsageMetrics(
                cpu_core_hours=total_cpu_seconds / 3600,  # convert to hours
                memory_gb_hours=total_memory_seconds / 3600,  # convert to hours
                s3_gb_hours=total_s3_hours,
                efs_gb_hours=total_efs_hours,
            ),
            record_count=len(usage_records),
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Error getting usage: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch usage: {str(e)}",
        )


@usage_router.get("/daily")
async def get_daily_usage(
    workspace_membership: tuple[UserWorkspacePydantic, WorkspacePydantic] = Depends(
        get_workspace_with_admin_access
    ),
    start_date: datetime | None = Query(
        None, description="Start date (defaults to start of current month)"
    ),
    end_date: datetime | None = Query(None, description="End date (defaults to now)"),
) -> DailyUsageResponse:
    """Get daily aggregated usage for the workspace, suitable for sparkline visualization."""
    _, workspace = workspace_membership

    try:
        now = datetime.now(timezone.utc)
        if not start_date:
            logger.info("No start date provided, using default start of current month")
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            logger.info("No end date provided, using default end of current month")
            end_date = now

        # Get all usage records for the period
        usage_records = await db.usage.get_workspace_usage(
            workspace_id=workspace.id,
            start_date=start_date,
            end_date=end_date,
        )

        # Group by day
        daily_data: dict[str, DailyUsageData] = {}
        for record in usage_records:
            day_key = record.collection_start.strftime("%Y-%m-%d")
            if day_key not in daily_data:
                daily_data[day_key] = DailyUsageData(
                    date=day_key,
                    cpu_core_hours=0.0,
                    memory_gb_hours=0.0,
                    s3_gb_hours=0.0,
                    efs_gb_hours=0.0,
                )

            daily_data[day_key].cpu_core_hours += record.cpu_core_seconds / 3600
            daily_data[day_key].memory_gb_hours += record.memory_gb_seconds / 3600
            daily_data[day_key].s3_gb_hours += record.s3_gb_hours
            daily_data[day_key].efs_gb_hours += record.efs_gb_hours

        # Fill in missing days with zeros
        current_date = start_date
        while current_date <= end_date:
            day_key = current_date.strftime("%Y-%m-%d")
            if day_key not in daily_data:
                daily_data[day_key] = DailyUsageData(
                    date=day_key,
                    cpu_core_hours=0.0,
                    memory_gb_hours=0.0,
                    s3_gb_hours=0.0,
                    efs_gb_hours=0.0,
                )
            current_date += timedelta(days=1)

        # Sort by date
        sorted_daily = sorted(daily_data.values(), key=lambda x: x.date)

        return DailyUsageResponse(
            workspace_id=workspace.id,
            period=UsagePeriodInfo(start=start_date, end=end_date),
            daily_usage=sorted_daily,
        )

    except Exception as e:
        logger.error(f"Error getting daily usage: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch daily usage: {str(e)}",
        )
