from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import (
    WorkspaceAccess,
    check_workspace_limit,
    get_workspace_with_admin_access,
    get_workspace_with_any_access,
)
from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.api.utils import normalize_usage_date_range
from lazycloud_api.database import db
from lazycloud_api.database.user_workspaces import (
    UserWorkspacePydantic,
    UserWorkspaceStatus,
    WorkspaceRole,
)
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.database.utils import validate_workspace_name
from lazycloud_api.database.workspaces import WorkspacePydantic, WorkspaceStatus
from lazycloud_api.services import (
    UsageService,
    get_usage_service,
)
from shared.requests.workspaces import (
    CreateWorkspaceRequest,
)
from shared.responses.deployments import DeploymentOverview
from shared.responses.usage import (
    AggregatedDailyUsageResponse,
    AggregatedUsageResponse,
)
from shared.responses.workspaces import (
    WorkspaceResponse,
    WorkspaceSuccessResponse,
    WorkspaceWithDeploymentsResponse,
)

workspaces_router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@workspaces_router.get("")
async def get_workspaces(
    current_user: UserPydantic = Depends(get_current_active_user),
    start_date: datetime | None = Query(
        None,
        description="Optional start date for usage context (includes deleted workspaces active during range)",
    ),
    end_date: datetime | None = Query(
        None, description="Optional end date for usage context"
    ),
) -> list[WorkspaceResponse]:
    """Get all workspaces the current user has access to.

    NOTE: If start_date and end_date are provided, inactive (deleted) workspaces
    that were active during the date range will be included.
    """
    if start_date and end_date:
        user_workspaces = await db.workspaces.aget_user_workspaces_active_during_range(
            user_id=current_user.id, start_date=start_date, end_date=end_date
        )
    else:
        user_workspaces = await db.workspaces.aget_user_workspaces_with_membership(
            current_user.id, status=WorkspaceStatus.ACTIVE
        )

    return [
        WorkspaceResponse(
            id=workspace.id,
            name=workspace.name,
            is_personal=workspace.is_personal,
            role=membership.role,
        )
        for workspace, membership in user_workspaces
    ]


@workspaces_router.get("/{workspace_id}/with-deployments")
async def get_workspace_with_deployments(
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_any_access),
    cursor: str | None = Query(None, description="Cursor to start from"),
    limit: int = Query(100, description="Limit the number of deployments returned"),
) -> WorkspaceWithDeploymentsResponse:
    """Get workspace with deployment overviews (including service/volume counts)"""
    workspace = workspace_access.workspace

    try:
        # Get all deployments for this workspace
        offset = int(cursor) if cursor else 0
        total, deployments = await db.compose_deployments.afind_paginated(
            filters={"workspace_id": workspace.id},
            offset=offset,
            limit=limit,
            include_deleted=False,
        )

        deployment_overviews: list[DeploymentOverview] = []

        for deployment in deployments:
            if not deployment.id or not deployment.name:
                continue

            # Calculate service, volume, and network counts from helm_values
            service_count = 0
            volume_count = 0
            network_count = 0

            if deployment.helm_values:
                if deployment.helm_values.services:
                    service_count = len(deployment.helm_values.services)
                if deployment.helm_values.volumes:
                    volume_count = len(deployment.helm_values.volumes)
                if deployment.helm_values.networks:
                    network_count = len(deployment.helm_values.networks)

            deployment_overviews.append(
                DeploymentOverview(
                    id=str(deployment.id),
                    workspace_id=str(deployment.workspace_id),
                    name=deployment.name,
                    namespace=deployment.namespace,
                    state=deployment.state,
                    status_message=deployment.status_message,
                    created_at=deployment.created_at,
                    updated_at=deployment.updated_at,
                    deployed_at=deployment.deployed_at,
                    service_count=service_count,
                    volume_count=volume_count,
                    network_count=network_count,
                    ready_services=None,  # Not available without K8s call
                )
            )

        return WorkspaceWithDeploymentsResponse(
            id=workspace.id,
            name=workspace.name,
            is_personal=workspace.is_personal,
            role=workspace_access.membership.role,
            deployments=deployment_overviews,
            cursor=str(offset + limit)
            if total is not None and total > offset + limit
            else None,
            has_more=total is not None and total > offset + limit,
        )

    except Exception as e:
        logger.error(
            f"Failed to get workspace with deployments for workspace {workspace.id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to fetch workspace with deployments"
        )


@workspaces_router.post("")
async def create_workspace(
    request: CreateWorkspaceRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
    _: None = Depends(check_workspace_limit),
) -> WorkspaceResponse:
    """Create a new workspace"""
    try:
        validated_name = validate_workspace_name(request.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        # Create workspace and membership in a single transaction
        async with db.workspaces.transaction() as session:
            workspace = WorkspacePydantic(
                name=validated_name,
                is_personal=False,
            )
            workspace = await db.workspaces.acreate(workspace, session=session)

            # Add current user as owner
            membership = UserWorkspacePydantic(
                user_id=current_user.id,
                workspace_id=workspace.id,
                role=WorkspaceRole.OWNER,
                status=UserWorkspaceStatus.ACTIVE,
            )
            membership = await db.user_workspaces.acreate(membership, session=session)

    except Exception:
        raise HTTPException(status_code=500, detail="Failed to create workspace")

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        is_personal=workspace.is_personal,
        role=membership.role,
    )


@workspaces_router.delete("/{workspace_id}")
async def delete_workspace(
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> WorkspaceSuccessResponse:
    """Delete a workspace (requires owner role)"""
    # Check if current user is the owner
    workspace = workspace_access.workspace

    # Prevent deleting personal workspaces
    if workspace.is_personal:
        raise HTTPException(status_code=400, detail="Cannot delete personal workspace")

    # get all deployments in the workspace
    deployments = await db.compose_deployments.afind({"workspace_id": workspace.id})
    if len(deployments) > 0:
        # delete all deployments for the workspace
        await db.compose_deployments.adelete_bulk(
            [deployment.id for deployment in deployments]
        )

    # set workspace status to deleted
    workspace = await db.workspaces.aupdate_status(
        workspace.id, WorkspaceStatus.DELETED
    )

    return WorkspaceSuccessResponse(success=True)


@workspaces_router.get("/usage/all")
async def get_aggregated_usage(
    current_user: UserPydantic = Depends(get_current_active_user),
    start_date: datetime | None = Query(
        None, description="Start date (defaults to start of current month)"
    ),
    end_date: datetime | None = Query(None, description="End date (defaults to now)"),
    usage_service: UsageService = Depends(get_usage_service),
) -> AggregatedUsageResponse:
    """Get aggregated usage across all user's workspaces with workspace summaries"""
    try:
        start_date, end_date = normalize_usage_date_range(start_date, end_date)
        return await usage_service.get_aggregated_usage_with_summaries(
            user_id=current_user.id,
            start_date=start_date,
            end_date=end_date,
            external_customer_id=current_user.clerk_id,
        )
    except Exception as e:
        logger.error(f"Error getting aggregated usage: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch aggregated usage: {str(e)}",
        )


@workspaces_router.get("/usage/all/daily")
async def get_aggregated_daily_usage(
    current_user: UserPydantic = Depends(get_current_active_user),
    start_date: datetime | None = Query(
        None, description="Start date (defaults to start of current month)"
    ),
    end_date: datetime | None = Query(None, description="End date (defaults to now)"),
    timezone_str: str = Query(
        "UTC", description="Timezone for grouping (e.g., 'America/Denver', 'UTC')"
    ),
    usage_service: UsageService = Depends(get_usage_service),
) -> AggregatedDailyUsageResponse:
    """Get aggregated daily usage across all user's workspaces"""
    try:
        start_date, end_date = normalize_usage_date_range(start_date, end_date)
        return await usage_service.get_aggregated_daily_usage(
            user_id=current_user.id,
            start_date=start_date,
            end_date=end_date,
            timezone_str=timezone_str,
            external_customer_id=current_user.clerk_id,
        )
    except Exception as e:
        logger.error(f"Error getting aggregated daily usage: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch aggregated daily usage: {str(e)}",
        )
