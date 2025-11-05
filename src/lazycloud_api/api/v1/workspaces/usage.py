from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import get_workspace_with_admin_access
from lazycloud_api.database import db
from lazycloud_api.database.usage import UsageRecordPydantic
from lazycloud_api.models.workspace_access import WorkspaceAccess
from lazycloud_api.services import (
    UsageService,
    get_cost_breakdown_service,
    get_polar_service,
    get_usage_service,
)
from shared.models.billing import SECONDS_PER_HOUR
from shared.responses.usage import (
    DailyUsageData,
    DailyUsageResponse,
    UsageMetrics,
    UsagePeriodInfo,
    WorkspaceCostBreakdownResponse,
    WorkspaceUsageResponse,
)

usage_router = APIRouter(prefix="/usage")


@usage_router.get("")
async def query_usage(
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
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
    workspace = workspace_access.workspace

    try:
        if deployment_id:
            deployment = await db.compose_deployments.aget_by_id(
                deployment_id, include_deleted=True
            )
            if not deployment or deployment.workspace_id != workspace.id:
                raise HTTPException(status_code=404, detail="Deployment not found")

            (
                metrics,
                services,
                volumes,
                usage_record,
            ) = await usage_service.get_workspace_usage_breakdown(
                workspace_id=workspace.id,
                deployment_id=deployment_id,
            )

            if not usage_record:
                raise HTTPException(status_code=404, detail="Usage record not found")

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

        # Return workspace-level aggregation across date range
        now = datetime.now(timezone.utc)
        if not start_date:
            logger.info("No start date provided, using default start of current month")
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            logger.info("No end date provided, using default end of current month")
            end_date = now

        usage_records = await db.usage.get_workspace_usage(
            workspace_id=workspace.id,
            start_date=start_date,
            end_date=end_date,
        )

        total_cpu_seconds = sum(r.cpu_core_seconds for r in usage_records)
        total_memory_seconds = sum(r.memory_gb_seconds for r in usage_records)
        total_s3_hours = sum(r.s3_gb_hours for r in usage_records)
        total_efs_hours = sum(r.efs_gb_hours for r in usage_records)

        # Calculate costs if Polar is enabled
        workspace_costs = None
        cost_service = get_cost_breakdown_service()
        polar_service = get_polar_service()

        if polar_service.enabled:
            try:
                workspace_costs = await cost_service.calculate_costs_from_usage(
                    cpu_core_hours=total_cpu_seconds / SECONDS_PER_HOUR,
                    memory_gb_hours=total_memory_seconds / SECONDS_PER_HOUR,
                    s3_gb_hours=total_s3_hours,
                    efs_gb_hours=total_efs_hours,
                    external_customer_id=workspace_access.user.clerk_id,
                )
            except Exception as e:
                logger.warning(f"Failed to calculate workspace costs: {e}")

        return WorkspaceUsageResponse(
            workspace_id=workspace.id,
            period=UsagePeriodInfo(start=start_date, end=end_date),
            usage=UsageMetrics(
                cpu_core_hours=total_cpu_seconds / SECONDS_PER_HOUR,
                memory_gb_hours=total_memory_seconds / SECONDS_PER_HOUR,
                s3_gb_hours=total_s3_hours,
                efs_gb_hours=total_efs_hours,
                costs=workspace_costs,
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
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
    start_date: datetime | None = Query(
        None, description="Start date (defaults to start of current month)"
    ),
    end_date: datetime | None = Query(None, description="End date (defaults to now)"),
) -> DailyUsageResponse:
    """Get daily aggregated usage for the workspace, suitable for sparkline visualization."""
    workspace = workspace_access.workspace

    try:
        now = datetime.now(timezone.utc)
        if not start_date:
            logger.info("No start date provided, using default start of current month")
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            logger.info("No end date provided, using default end of current month")
            end_date = now

        usage_records = await db.usage.get_workspace_usage(
            workspace_id=workspace.id,
            start_date=start_date,
            end_date=end_date,
        )

        daily_data: dict[str, DailyUsageData] = {}
        daily_records: dict[str, list[UsageRecordPydantic]] = {}
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
                daily_records[day_key] = []

            daily_data[day_key].cpu_core_hours += (
                record.cpu_core_seconds / SECONDS_PER_HOUR
            )
            daily_data[day_key].memory_gb_hours += (
                record.memory_gb_seconds / SECONDS_PER_HOUR
            )
            daily_data[day_key].s3_gb_hours += record.s3_gb_hours
            daily_data[day_key].efs_gb_hours += record.efs_gb_hours
            daily_records[day_key].append(record)

        # Fill missing days with zeros for continuous sparkline visualization
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


@usage_router.get("/costs")
async def get_cost_breakdown(
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
    start_date: datetime | None = Query(
        None, description="Start date (defaults to start of current month)"
    ),
    end_date: datetime | None = Query(None, description="End date (defaults to now)"),
    deployment_id: str | None = Query(
        None, description="Deployment ID to get detailed cost breakdown for"
    ),
) -> WorkspaceCostBreakdownResponse:
    """Get estimated cost breakdown for workspace usage"""
    workspace = workspace_access.workspace
    user = workspace_access.user

    try:
        polar_service = get_polar_service()
        cost_service = get_cost_breakdown_service()

        if not polar_service.enabled:
            raise HTTPException(
                status_code=503,
                detail="Cost breakdown unavailable: billing service not configured",
            )

        # Get customer's external ID (clerk_id) - already available from dependency
        external_customer_id = user.clerk_id

        # Handle deployment-specific or date range breakdown
        if deployment_id:
            return await cost_service.get_deployment_cost_breakdown(
                workspace_id=workspace.id,
                deployment_id=deployment_id,
                external_customer_id=external_customer_id,
            )

        # Default date range to current month if not specified
        now = datetime.now(timezone.utc)
        if not start_date:
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            end_date = now

        return await cost_service.get_aggregated_cost_breakdown(
            workspace_id=workspace.id,
            start_date=start_date,
            end_date=end_date,
            external_customer_id=external_customer_id,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Error getting cost breakdown: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch cost breakdown: {str(e)}",
        )
