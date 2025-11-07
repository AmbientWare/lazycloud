from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import get_workspace_with_admin_access_for_usage
from lazycloud_api.database import db
from lazycloud_api.models.workspace_access import WorkspaceAccess
from lazycloud_api.services import (
    CostBreakdownService,
    PolarService,
    UsageService,
    get_cost_breakdown_service,
    get_polar_service,
    get_usage_service,
)
from shared.responses.usage import (
    DeploymentUsageOverview,
    UsagePeriodInfo,
    WorkspaceUsageWithDeploymentsResponse,
)

usage_router = APIRouter(prefix="/usage")

# Constants
MAX_DEPLOYMENT_LIMIT = 100
MAX_DATE_RANGE_DAYS = 365  # 1 year maximum


@usage_router.get("/with-deployments")
async def get_workspace_usage_with_deployments(
    workspace_access: WorkspaceAccess = Depends(
        get_workspace_with_admin_access_for_usage
    ),
    start_date: datetime | None = Query(
        None, description="Start date (defaults to start of current month)"
    ),
    end_date: datetime | None = Query(None, description="End date (defaults to now)"),
    usage_service: UsageService = Depends(get_usage_service),
    cost_service: CostBreakdownService = Depends(get_cost_breakdown_service),
    polar_service: PolarService = Depends(get_polar_service),
) -> WorkspaceUsageWithDeploymentsResponse:
    """Get workspace usage with deployment breakdowns in a single request"""
    workspace = workspace_access.workspace
    user = workspace_access.user

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
                f"for workspace {workspace.id}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Date range cannot exceed {MAX_DATE_RANGE_DAYS} days",
            )

        # Get workspace-level usage and records in a single query
        (
            workspace_usage,
            usage_records,
        ) = await usage_service.aggregate_workspace_usage_for_date_range(
            workspace_id=workspace.id,
            start_date=start_date,
            end_date=end_date,
            return_records=True,
        )

        # Calculate workspace costs if Polar is enabled
        workspace_costs = None
        if polar_service.enabled:
            try:
                workspace_costs = await cost_service.calculate_costs_from_usage(
                    cpu_core_hours=workspace_usage.cpu_core_hours,
                    memory_gb_hours=workspace_usage.memory_gb_hours,
                    s3_gb_hours=workspace_usage.s3_gb_hours,
                    efs_gb_hours=workspace_usage.efs_gb_hours,
                    external_customer_id=user.clerk_id,
                )
            except Exception as e:
                logger.warning(
                    f"Failed to calculate workspace costs for workspace {workspace.id}: {e}",
                    exc_info=True,
                )

        workspace_usage.costs = workspace_costs

        # Get all deployments for this workspace that existed during the date range
        # Include deleted deployments if they were deleted during or after the date range
        deployments = await db.compose_deployments.afind_active_during_date_range(
            workspace_id=str(workspace.id),
            start_date=start_date,
            end_date=end_date,
            limit=MAX_DEPLOYMENT_LIMIT,
        )
        total_deployments = len(deployments)

        if total_deployments > MAX_DEPLOYMENT_LIMIT:
            logger.warning(
                f"Workspace {workspace.id} has {total_deployments} deployments, "
                f"but only {MAX_DEPLOYMENT_LIMIT} are included in usage breakdown. "
                f"Consider pagination or increasing MAX_DEPLOYMENT_LIMIT."
            )

        # Get usage overview for each deployment
        # Aggregate from usage records' breakdown tables for the date range
        deployment_overviews: list[DeploymentUsageOverview] = []

        for deployment in deployments:
            if not deployment.id or not deployment.name:
                continue

            try:
                deployment_metrics, _, _ = (
                    usage_service.aggregate_deployment_usage_from_records(
                        usage_records=usage_records,
                        deployment_id=str(deployment.id),
                    )
                )

                # Skip if no usage found
                if (
                    deployment_metrics.cpu_core_hours == 0
                    and deployment_metrics.memory_gb_hours == 0
                    and deployment_metrics.s3_gb_hours == 0
                    and deployment_metrics.efs_gb_hours == 0
                ):
                    continue

                # Calculate deployment meter costs only (no service/volume breakdown)
                deployment_costs = None

                if polar_service.enabled:
                    try:
                        deployment_costs = (
                            await cost_service.calculate_costs_from_usage(
                                cpu_core_hours=deployment_metrics.cpu_core_hours,
                                memory_gb_hours=deployment_metrics.memory_gb_hours,
                                s3_gb_hours=deployment_metrics.s3_gb_hours,
                                efs_gb_hours=deployment_metrics.efs_gb_hours,
                                external_customer_id=user.clerk_id,
                            )
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to calculate costs for deployment {deployment.name} "
                            f"(id: {deployment.id}) in workspace {workspace.id}: {e}",
                            exc_info=True,
                        )

                deployment_metrics.costs = deployment_costs

                # Determine deployment status based on deleted_at
                deployment_status = (
                    "Active" if deployment.deleted_at is None else "Inactive"
                )

                deployment_overviews.append(
                    DeploymentUsageOverview(
                        deployment_id=str(deployment.id),
                        deployment_name=deployment.name,
                        usage=deployment_metrics,
                        status=deployment_status,
                    )
                )
            except Exception as e:
                logger.warning(
                    f"Error processing deployment {deployment.name or 'unknown'} "
                    f"(id: {deployment.id}) in workspace {workspace.id}: {e}",
                    exc_info=True,
                )
                continue

        # Determine workspace status based on deleted_at
        workspace_status = "Active" if workspace.deleted_at is None else "Inactive"

        return WorkspaceUsageWithDeploymentsResponse(
            workspace_id=workspace.id,
            period=UsagePeriodInfo(start=start_date, end=end_date),
            workspace_usage=workspace_usage,
            record_count=len(usage_records),
            deployments=deployment_overviews,
            workspace_status=workspace_status,
        )

    except HTTPException:
        raise

    except ValueError as e:
        logger.error(
            f"Invalid input for workspace usage with deployments (workspace_id: {workspace.id}): {e}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid request: {str(e)}",
        )

    except Exception as e:
        logger.error(
            f"Unexpected error getting workspace usage with deployments "
            f"for workspace {workspace.id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch workspace usage with deployments",
        )
