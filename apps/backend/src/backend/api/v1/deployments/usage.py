from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from responses.usage import (
    UsagePeriodInfo,
    WorkspaceCostBreakdownResponse,
)

from backend.api.dependencies import get_deployment_with_admin_access_for_usage
from backend.api.security import get_current_active_user
from backend.database.compose import ComposeDeploymentPydantic
from backend.database.users import UserPydantic
from backend.services import (
    PolarService,
    UsageService,
    get_polar_service,
    get_usage_service,
)

usage_router = APIRouter(prefix="/{deployment_id}/usage")

MAX_DATE_RANGE_DAYS = 365


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
        now = datetime.now(timezone.utc)
        if not start_date:
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        if not end_date:
            end_date = now

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

        # Get deployment breakdown from usage events
        (
            deployment_metrics,
            service_usage_list,
            volume_usage_list,
        ) = await usage_service.get_deployment_breakdown(
            workspace_id=deployment.workspace_id,
            deployment_id=deployment.id,
            start_date=start_date,
            end_date=end_date,
        )

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
                    standard_gb_hours=deployment_metrics.standard_gb_hours,
                    shared_gb_hours=deployment_metrics.shared_gb_hours,
                    build_minutes=deployment_metrics.build_minutes,
                    public_endpoint_hours=deployment_metrics.public_endpoint_hours,
                    service_usage=service_usage_list if service_usage_list else None,
                    volume_usage=volume_usage_list if volume_usage_list else None,
                )
            )

            return WorkspaceCostBreakdownResponse(
                workspace_id=deployment.workspace_id,
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
