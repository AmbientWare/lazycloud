from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import get_deployment_with_admin_access_for_usage
from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.services import (
    PolarService,
    UsageService,
    get_polar_service,
    get_usage_service,
)
from shared.models.billing import UsageCollectionConfig
from shared.responses.usage import (
    UsagePeriodInfo,
    WorkspaceCostBreakdownResponse,
)

usage_router = APIRouter(prefix="/{deployment_id}/usage")

# Constants
MAX_DATE_RANGE_DAYS = 365  # 1 year maximum


@usage_router.get("/breakdown")
async def get_deployment_cost_breakdown(
    deployment: ComposeDeploymentPydantic = Depends(
        get_deployment_with_admin_access_for_usage
    ),
    current_user: UserPydantic = Depends(get_current_active_user),
    start_date: datetime | None = Query(
        None, description="Start date (defaults to start of current month)"
    ),
    end_date: datetime | None = Query(None, description="End date (defaults to now)"),
    usage_service: UsageService = Depends(get_usage_service),
    polar_service: PolarService = Depends(get_polar_service),
) -> WorkspaceCostBreakdownResponse:
    """Get detailed cost breakdown for a specific deployment with service and volume details"""
    try:
        # Default date range to current month if not specified
        now = datetime.now(timezone.utc)
        if not start_date:
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            end_date = now

        # Validate date range
        if start_date >= end_date:
            raise HTTPException(
                status_code=400,
                detail="start_date must be before end_date",
            )

        date_range_days = (end_date - start_date).days
        if date_range_days > MAX_DATE_RANGE_DAYS:
            logger.warning(
                f"Date range of {date_range_days} days exceeds maximum of {MAX_DATE_RANGE_DAYS} days "
                f"for deployment {deployment.id}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Date range cannot exceed {MAX_DATE_RANGE_DAYS} days",
            )

        # Get usage records for the date range
        usage_records = await db.usage.get_workspace_usage(
            workspace_id=str(deployment.workspace_id),
            start_date=start_date,
            end_date=end_date,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        if not usage_records:
            raise HTTPException(
                status_code=404,
                detail="No usage records found for the specified date range",
            )

        # Aggregate deployment usage from records
        deployment_metrics, service_usage_list, volume_usage_list = (
            usage_service.aggregate_deployment_usage_from_records(
                usage_records=usage_records,
                deployment_id=str(deployment.id),
            )
        )

        # Calculate full cost breakdown with service and volume details
        if not polar_service.enabled:
            raise HTTPException(
                status_code=400,
                detail="Polar service is not enabled. Cost breakdowns are not available.",
            )

        try:
            workspace_cost_breakdown = (
                await polar_service.cost_breakdown.calculate_workspace_costs(
                    external_customer_id=current_user.clerk_id,
                    cpu_core_hours=deployment_metrics.cpu_core_hours,
                    memory_gb_hours=deployment_metrics.memory_gb_hours,
                    s3_gb_hours=deployment_metrics.s3_gb_hours,
                    efs_gb_hours=deployment_metrics.efs_gb_hours,
                    service_usage=service_usage_list if service_usage_list else None,
                    volume_usage=volume_usage_list if volume_usage_list else None,
                )
            )

            return WorkspaceCostBreakdownResponse(
                workspace_id=str(deployment.workspace_id),
                period=UsagePeriodInfo(start=start_date, end=end_date),
                meter_breakdown=workspace_cost_breakdown.meter_breakdown,
                service_breakdown=workspace_cost_breakdown.service_breakdown,
                volume_breakdown=workspace_cost_breakdown.volume_breakdown,
                is_estimated=True,
            )

        except Exception as e:
            logger.error(
                f"Failed to calculate cost breakdown for deployment {deployment.id}: {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to calculate cost breakdown: {str(e)}",
            )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(
            f"Unexpected error getting deployment cost breakdown "
            f"for deployment {deployment.id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch deployment cost breakdown",
        )
