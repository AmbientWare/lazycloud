from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException

from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.billing.product_details.features import BaseFeatures
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.user_workspaces import WorkspaceRole
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.models.workspace_access import WorkspaceAccess
from lazycloud_api.services import get_subscription_service
from lazycloud_api.services.subscription_service import SubscriptionLimitError

if TYPE_CHECKING:
    from shared.models.compose import ComposeFile


async def require_workspace_member(
    workspace_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> None:
    """Verify user is a member of workspace (any role)"""
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")


async def require_workspace_admin(
    workspace_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> None:
    """Verify user is admin or owner of workspace"""
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")
    if membership.role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required")


async def get_workspace_with_any_access(
    workspace_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> WorkspaceAccess:
    """Get workspace and verify user has any access"""
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")

    workspace = await db.workspaces.get_by_id(workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")

    return WorkspaceAccess(
        membership=membership, workspace=workspace, user=current_user
    )


async def get_workspace_with_admin_access(
    workspace_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> WorkspaceAccess:
    """Get workspace and verify user has admin/owner access"""
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")
    if membership.role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required")

    workspace = await db.workspaces.get_by_id(workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")

    return WorkspaceAccess(
        membership=membership, workspace=workspace, user=current_user
    )


async def get_workspace_with_owner_access(
    workspace_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> WorkspaceAccess:
    """Get workspace and verify user has owner access"""
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")

    if membership.role != WorkspaceRole.OWNER:
        raise HTTPException(403, "Owner role required")

    workspace = await db.workspaces.get_by_id(workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")

    return WorkspaceAccess(
        membership=membership, workspace=workspace, user=current_user
    )


async def get_workspace_with_admin_access_for_usage(
    workspace_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> WorkspaceAccess:
    """Get workspace and verify user has admin/owner access, including deleted workspaces (for usage reporting)"""
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")
    if membership.role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required")

    workspace = await db.workspaces.get_by_id(workspace_id, include_deleted=True)
    if not workspace:
        raise HTTPException(404, "Workspace not found")

    return WorkspaceAccess(
        membership=membership, workspace=workspace, user=current_user
    )


async def get_deployment_with_access(
    deployment_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> ComposeDeploymentPydantic:
    """Get deployment and verify user has access (any role)"""
    deployment, role = await db.compose_deployments.get_with_workspace_access(
        deployment_id, current_user.id
    )

    if not deployment or not role:
        raise HTTPException(404, "Deployment not found")

    return deployment


async def get_deployment_with_admin_access(
    deployment_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
    include_deleted: bool = False,
) -> ComposeDeploymentPydantic:
    """Get deployment and verify user has admin/owner access"""
    deployment, role = await db.compose_deployments.get_with_workspace_access(
        deployment_id, current_user.id, include_deleted=include_deleted
    )

    if not deployment or not role:
        raise HTTPException(404, "Deployment not found")

    if role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required")

    return deployment


async def get_deployment_with_admin_access_for_usage(
    deployment_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> ComposeDeploymentPydantic:
    """Get deployment with admin access, including deleted deployments for usage purposes"""
    return await get_deployment_with_admin_access(
        deployment_id, current_user, include_deleted=True
    )


async def get_user_product_features(
    current_user: UserPydantic = Depends(get_current_active_user),
) -> BaseFeatures:
    """Get product features for the current user based on their subscription."""
    subscription_service = get_subscription_service()
    return await subscription_service.get_user_features(
        external_customer_id=current_user.clerk_id
    )


async def check_workspace_limit(
    current_user: UserPydantic = Depends(get_current_active_user),
    features: BaseFeatures = Depends(get_user_product_features),
) -> None:
    """Check if user can create a new workspace based on their subscription tier."""
    subscription_service = get_subscription_service()
    try:
        await subscription_service.check_workspace_limit(current_user.id, features)

    except SubscriptionLimitError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


async def check_deployment_limit(
    workspace_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
    features: BaseFeatures = Depends(get_user_product_features),
) -> None:
    """Check if user can create a new deployment in the workspace based on their subscription tier."""
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")

    subscription_service = get_subscription_service()
    try:
        await subscription_service.check_deployment_limit(
            workspace_id, features, user_id=current_user.id
        )

    except SubscriptionLimitError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


async def check_deployment_features(
    compose_file: "ComposeFile",
    compose_data: dict[str, Any],
    features: BaseFeatures,
) -> None:
    """Check if deployment features (services, volumes, networks, domains) are within subscription limits."""
    subscription_service = get_subscription_service()
    try:
        await subscription_service.check_deployment_features(
            compose_file, compose_data, features
        )

    except SubscriptionLimitError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
