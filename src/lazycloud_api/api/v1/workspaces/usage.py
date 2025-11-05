from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import get_workspace_with_admin_access
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
    DeploymentUsageBreakdown,
    UsagePeriodInfo,
    WorkspaceCostBreakdownResponse,
    WorkspaceUsageWithDeploymentsResponse,
)

usage_router = APIRouter(prefix="/usage")

# Constants
MAX_DEPLOYMENT_LIMIT = 100
MAX_DATE_RANGE_DAYS = 365  # 1 year maximum


@usage_router.get("/with-deployments")
async def get_workspace_usage_with_deployments(
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
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

        # Get all deployments for this workspace
        deployments_list = await db.compose_deployments.afind_paginated(
            filters={"workspace_id": workspace.id},
            skip=0,
            limit=MAX_DEPLOYMENT_LIMIT,
            include_deleted=False,
        )
        total_deployments, deployments = deployments_list

        if total_deployments > MAX_DEPLOYMENT_LIMIT:
            logger.warning(
                f"Workspace {workspace.id} has {total_deployments} deployments, "
                f"but only {MAX_DEPLOYMENT_LIMIT} are included in usage breakdown. "
                f"Consider pagination or increasing MAX_DEPLOYMENT_LIMIT."
            )

        # Get usage and cost breakdown for each deployment
        # Aggregate from usage records' breakdown tables for the date range
        deployment_breakdowns: list[DeploymentUsageBreakdown] = []

        for deployment in deployments:
            if not deployment.id or not deployment.name:
                continue

            try:
                deployment_metrics, service_usage_list, volume_usage_list = (
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

                # Calculate deployment costs and breakdown if Polar is enabled
                deployment_costs = None
                cost_breakdown = None

                if polar_service.enabled:
                    try:
                        workspace_cost_breakdown = await polar_service.cost_breakdown.calculate_workspace_costs(
                            external_customer_id=user.clerk_id,
                            cpu_core_hours=deployment_metrics.cpu_core_hours,
                            memory_gb_hours=deployment_metrics.memory_gb_hours,
                            s3_gb_hours=deployment_metrics.s3_gb_hours,
                            efs_gb_hours=deployment_metrics.efs_gb_hours,
                            service_usage=service_usage_list
                            if service_usage_list
                            else None,
                            volume_usage=volume_usage_list
                            if volume_usage_list
                            else None,
                        )

                        deployment_costs = workspace_cost_breakdown.meter_breakdown
                        cost_breakdown = WorkspaceCostBreakdownResponse(
                            workspace_id=workspace.id,
                            period=UsagePeriodInfo(start=start_date, end=end_date),
                            meter_breakdown=workspace_cost_breakdown.meter_breakdown,
                            service_breakdown=workspace_cost_breakdown.service_breakdown,
                            volume_breakdown=workspace_cost_breakdown.volume_breakdown,
                            is_estimated=True,
                        )

                    except Exception as e:
                        logger.warning(
                            f"Failed to calculate costs for deployment {deployment.name} "
                            f"(id: {deployment.id}) in workspace {workspace.id}: {e}",
                            exc_info=True,
                        )

                deployment_metrics.costs = deployment_costs

                deployment_breakdowns.append(
                    DeploymentUsageBreakdown(
                        deployment_id=str(deployment.id),
                        deployment_name=deployment.name,
                        usage=deployment_metrics,
                        cost_breakdown=cost_breakdown,
                    )
                )
            except Exception as e:
                logger.warning(
                    f"Error processing deployment {deployment.name or 'unknown'} "
                    f"(id: {deployment.id}) in workspace {workspace.id}: {e}",
                    exc_info=True,
                )
                continue

        return WorkspaceUsageWithDeploymentsResponse(
            workspace_id=workspace.id,
            period=UsagePeriodInfo(start=start_date, end=end_date),
            workspace_usage=workspace_usage,
            record_count=len(usage_records),
            deployments=deployment_breakdowns,
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
