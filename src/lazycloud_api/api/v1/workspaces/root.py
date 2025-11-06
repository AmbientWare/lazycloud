from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from lazycloud_api.api.dependencies import (
    WorkspaceAccess,
    get_workspace_with_admin_access,
    get_workspace_with_any_access,
)
from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.api.utils import (
    get_calendar_day_in_timezone,
    get_utc_midnight_for_calendar_day,
)
from lazycloud_api.database import db
from lazycloud_api.database.usage import UsageRecordPydantic
from lazycloud_api.database.user_workspaces import (
    UserWorkspacePydantic,
    UserWorkspaceStatus,
    WorkspaceRole,
)
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.database.utils import validate_workspace_name
from lazycloud_api.database.workspaces import WorkspacePydantic, WorkspaceStatus
from lazycloud_api.services import (
    CostBreakdownService,
    PolarService,
    get_cost_breakdown_service,
    get_polar_service,
)
from shared.models.billing import (
    SECONDS_PER_HOUR,
    UsageCollectionConfig,
)
from shared.requests.workspaces import (
    CreateWorkspaceRequest,
    InviteUserRequest,
    UpdateMemberRoleRequest,
)
from shared.responses.deployments import DeploymentOverview
from shared.responses.usage import (
    AggregatedDailyUsageResponse,
    AggregatedUsageResponse,
    DailyUsageData,
    UsageMetrics,
    UsagePeriodInfo,
)
from shared.responses.workspaces import (
    WorkspaceMemberResponse,
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

    If start_date and end_date are provided, includes deleted workspaces
    that were active during the date range (for usage reporting).
    """
    if start_date and end_date:
        # For usage context, include deleted workspaces active during the range
        user_workspaces = await db.workspaces.aget_user_workspaces_active_during_range(
            user_id=current_user.id,
            start_date=start_date,
            end_date=end_date,
        )
    else:
        # Default: only show active workspaces
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
) -> WorkspaceWithDeploymentsResponse:
    """Get workspace with deployment overviews (including service/volume counts)"""
    workspace = workspace_access.workspace

    try:
        # Get all deployments for this workspace
        total, deployments = await db.compose_deployments.afind_paginated(
            filters={"workspace_id": workspace.id},
            skip=0,
            limit=100,
            include_deleted=False,
        )

        deployment_overviews: list[DeploymentOverview] = []

        for deployment in deployments:
            if not deployment.id or not deployment.name:
                continue

            # Calculate service and volume counts from helm_values
            service_count = 0
            volume_count = 0

            if deployment.helm_values:
                if deployment.helm_values.services:
                    service_count = len(deployment.helm_values.services)
                if deployment.helm_values.volumes:
                    volume_count = len(deployment.helm_values.volumes)

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
                    ready_services=None,  # Not available without K8s call
                )
            )

        return WorkspaceWithDeploymentsResponse(
            id=workspace.id,
            name=workspace.name,
            is_personal=workspace.is_personal,
            role=workspace_access.membership.role,
            deployments=deployment_overviews,
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


@workspaces_router.post("/{workspace_id}/invite")
async def invite_user(
    request: InviteUserRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> WorkspaceSuccessResponse:
    """Invite a user to a workspace (requires owner or admin role)"""
    workspace = workspace_access.workspace

    if workspace.is_personal:
        raise HTTPException(
            status_code=400, detail="Cannot invite users to personal workspaces"
        )

    # TODO: Send email to user with invite link
    logger.info(
        f"Inviting user with email: {request.email} to workspace: {workspace.name} (ID: {workspace.id}) with role: {request.role.value}"
    )

    return WorkspaceSuccessResponse(success=True)


@workspaces_router.get("/{workspace_id}/members")
async def get_workspace_members(
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_any_access),
) -> list[WorkspaceMemberResponse]:
    """Get all members of a workspace"""
    workspace = workspace_access.workspace

    # Get all members with user information
    members_with_users = await db.user_workspaces.aget_workspace_members_with_users(
        workspace.id
    )

    return [
        WorkspaceMemberResponse(
            user_id=member.user_id,
            name=user.name,
            email=user.email,
            role=member.role,
            status=member.status,
        )
        for member, user in members_with_users
    ]


@workspaces_router.patch("/{workspace_id}/members/{user_id}/role")
async def update_member_role(
    request: UpdateMemberRoleRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> WorkspaceMemberResponse:
    """Update a member's role (requires owner or admin role)"""
    workspace = workspace_access.workspace

    target_membership = await db.user_workspaces.aget_by_user_and_workspace(
        request.user_id, workspace.id
    )
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member")

    # Prevent changing owner role (can only be changed via transfer)
    if target_membership.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=400,
            detail="Cannot change owner role. Use transfer ownership instead.",
        )

    # Prevent setting role to owner (can only be done via transfer)
    if request.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=400,
            detail="Cannot set role to owner. Use transfer ownership instead.",
        )

    # Update the role
    target_membership.role = request.role
    updated_membership = await db.user_workspaces.aupdate(target_membership)

    # Get user information for response
    user = await db.users.aget_by_id(request.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return WorkspaceMemberResponse(
        user_id=updated_membership.user_id,
        name=user.name,
        email=user.email,
        role=updated_membership.role,
        status=updated_membership.status,
    )


@workspaces_router.delete("/{workspace_id}/members/{user_id}")
async def remove_member(
    user_id: str,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> WorkspaceSuccessResponse:
    """Remove a member from a workspace (requires owner or admin role)"""
    # Check if current user has permission
    workspace = workspace_access.workspace

    # Check if target user is a member
    target_membership = await db.user_workspaces.aget_by_user_and_workspace(
        user_id, workspace.id
    )
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member")

    # Prevent removing the owner
    if target_membership.role == WorkspaceRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot remove the owner")

    # Remove the member
    await db.user_workspaces.adelete(target_membership.id)

    return WorkspaceSuccessResponse(success=True)


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
) -> AggregatedUsageResponse:
    """Get aggregated usage across all user's workspaces"""
    try:
        now = datetime.now(timezone.utc)
        if not start_date:
            logger.info("No start date provided, using default start of current month")
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            logger.info("No end date provided, using default end of current month")
            end_date = now

        # Get all workspaces that were active during the date range
        # Includes active workspaces and deleted workspaces deleted during/after the range
        all_user_workspaces = (
            await db.workspaces.aget_user_workspaces_active_during_range(
                user_id=current_user.id,
                start_date=start_date,
                end_date=end_date,
            )
        )

        total_cpu_seconds = 0.0
        total_memory_seconds = 0.0
        total_s3_hours = 0.0
        total_efs_hours = 0.0
        total_records = 0
        all_usage_records = []

        for workspace, _ in all_user_workspaces:
            usage_records = await db.usage.get_workspace_usage(
                workspace_id=workspace.id,
                start_date=start_date,
                end_date=end_date,
                record_type=UsageCollectionConfig.get_record_type(),
            )

            total_cpu_seconds += sum(r.cpu_core_seconds for r in usage_records)
            total_memory_seconds += sum(r.memory_gb_seconds for r in usage_records)
            total_s3_hours += sum(r.s3_gb_hours for r in usage_records)
            total_efs_hours += sum(r.efs_gb_hours for r in usage_records)
            total_records += len(usage_records)
            all_usage_records.extend(usage_records)

        # Calculate costs if Polar is enabled
        total_costs = None
        cost_service = get_cost_breakdown_service()
        polar_service = get_polar_service()

        if polar_service.enabled:
            try:
                total_costs = await cost_service.calculate_costs_from_usage(
                    cpu_core_hours=total_cpu_seconds / SECONDS_PER_HOUR,
                    memory_gb_hours=total_memory_seconds / SECONDS_PER_HOUR,
                    s3_gb_hours=total_s3_hours,
                    efs_gb_hours=total_efs_hours,
                    external_customer_id=current_user.clerk_id,
                )
            except Exception as e:
                logger.warning(f"Failed to calculate aggregated costs: {e}")

        return AggregatedUsageResponse(
            period=UsagePeriodInfo(start=start_date, end=end_date),
            usage=UsageMetrics(
                cpu_core_hours=total_cpu_seconds / SECONDS_PER_HOUR,
                memory_gb_hours=total_memory_seconds / SECONDS_PER_HOUR,
                s3_gb_hours=total_s3_hours,
                efs_gb_hours=total_efs_hours,
                costs=total_costs,
            ),
            workspace_count=len(all_user_workspaces),
            record_count=total_records,
        )

    except Exception as e:
        logger.error(f"Error getting aggregated usage: {e}")
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
    cost_service: CostBreakdownService = Depends(get_cost_breakdown_service),
    polar_service: PolarService = Depends(get_polar_service),
) -> AggregatedDailyUsageResponse:
    """Get aggregated daily usage across all user's workspaces"""
    try:
        # Parse timezone, default to UTC if invalid
        try:
            tz = ZoneInfo(timezone_str) if timezone_str else ZoneInfo("UTC")
        except Exception:
            logger.warning(f"Invalid timezone '{timezone_str}', defaulting to UTC")
            tz = ZoneInfo("UTC")

        now = datetime.now(timezone.utc)
        if not start_date:
            logger.info("No start date provided, using default start of current month")
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            logger.info("No end date provided, using default end of current month")
            end_date = now

        # Get all workspaces that were active during the date range
        # Includes active workspaces and deleted workspaces deleted during/after the range
        user_workspaces = await db.workspaces.aget_user_workspaces_active_during_range(
            user_id=current_user.id,
            start_date=start_date,
            end_date=end_date,
        )

        daily_data: dict[str, DailyUsageData] = {}
        daily_records: dict[str, list[UsageRecordPydantic]] = {}

        for workspace, _ in user_workspaces:
            usage_records = await db.usage.get_workspace_usage(
                workspace_id=workspace.id,
                start_date=start_date,
                end_date=end_date,
                record_type=UsageCollectionConfig.get_record_type(),
            )

            for record in usage_records:
                # Group by calendar day in user's timezone
                day_key = get_calendar_day_in_timezone(record.collection_start, tz)
                if day_key not in daily_data:
                    # Store UTC midnight for this calendar day in user's timezone
                    utc_midnight = get_utc_midnight_for_calendar_day(day_key, tz)
                    daily_data[day_key] = DailyUsageData(
                        date=utc_midnight.strftime("%Y-%m-%dT%H:%M:%SZ"),
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

        # Fill in missing days with zeros (but not future days)
        now_local = now.astimezone(tz)
        start_local = start_date.astimezone(tz).date()
        end_local = min(end_date.astimezone(tz).date(), now_local.date())
        current_local_date = start_local

        while current_local_date <= end_local:
            day_key = current_local_date.strftime("%Y-%m-%d")
            if day_key not in daily_data:
                # Only add zero entries for days that have already occurred
                utc_midnight = get_utc_midnight_for_calendar_day(day_key, tz)
                daily_data[day_key] = DailyUsageData(
                    date=utc_midnight.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    cpu_core_hours=0.0,
                    memory_gb_hours=0.0,
                    s3_gb_hours=0.0,
                    efs_gb_hours=0.0,
                )
            current_local_date += timedelta(days=1)

        if polar_service.enabled:
            for day_key, day_data in daily_data.items():
                try:
                    day_costs = await cost_service.calculate_costs_from_usage(
                        cpu_core_hours=day_data.cpu_core_hours,
                        memory_gb_hours=day_data.memory_gb_hours,
                        s3_gb_hours=day_data.s3_gb_hours,
                        efs_gb_hours=day_data.efs_gb_hours,
                        external_customer_id=current_user.clerk_id,
                    )
                    day_data.costs = day_costs
                except Exception as e:
                    logger.warning(f"Failed to calculate costs for {day_key}: {e}")

        sorted_daily = sorted(daily_data.values(), key=lambda x: x.date)

        return AggregatedDailyUsageResponse(
            period=UsagePeriodInfo(start=start_date, end=end_date),
            daily_usage=sorted_daily,
            workspace_count=len(user_workspaces),
        )

    except Exception as e:
        logger.error(f"Error getting aggregated daily usage: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch aggregated daily usage: {str(e)}",
        )
