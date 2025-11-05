from fastapi import Depends, HTTPException

from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.user_workspaces import WorkspaceRole
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.models.workspace_access import WorkspaceAccess


async def require_workspace_member(
    workspace_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> None:
    """Verify user is a member of workspace (any role)"""
    membership = await db.user_workspaces.aget_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")


async def require_workspace_admin(
    workspace_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> None:
    """Verify user is admin or owner of workspace"""
    membership = await db.user_workspaces.aget_by_user_and_workspace(
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
    membership = await db.user_workspaces.aget_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")

    workspace = await db.workspaces.aget_by_id(workspace_id)
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
    membership = await db.user_workspaces.aget_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")
    if membership.role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required")

    workspace = await db.workspaces.aget_by_id(workspace_id)
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
    deployment, role = await db.compose_deployments.aget_with_workspace_access(
        deployment_id, current_user.id
    )

    if not deployment or not role:
        raise HTTPException(404, "Deployment not found")

    return deployment


async def get_deployment_with_admin_access(
    deployment_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> ComposeDeploymentPydantic:
    """Get deployment and verify user has admin/owner access"""
    deployment, role = await db.compose_deployments.aget_with_workspace_access(
        deployment_id, current_user.id
    )

    if not deployment or not role:
        raise HTTPException(404, "Deployment not found")

    if role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required")

    return deployment


async def check_deployment_exists_with_admin_access(
    deployment_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> ComposeDeploymentPydantic | None:
    """Check if deployment exists and user has admin/owner access"""
    deployment, role = await db.compose_deployments.aget_with_workspace_access(
        deployment_id, current_user.id
    )

    if role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required")

    return deployment
