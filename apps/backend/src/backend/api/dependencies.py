from fastapi import Depends, HTTPException
from models.compose import ComposeFile
from pydantic import BaseModel

from backend.api.security import get_current_active_user
from backend.billing.product_details.base import DEVELOPER_FEATURES
from backend.billing.product_details.features import ADMIN_FEATURES, BaseFeatures
from backend.database import Database, get_db
from backend.database.models import (
    ComposeDeploymentInDb,
    UserInDb,
    UserRole,
    UserWorkspaceInDb,
    WorkspaceInDb,
    WorkspaceRole,
)
from backend.services import get_polar_service, get_subscription_service
from backend.services.exceptions import NoActiveSubscriptionError
from backend.services.subscription_service import (
    BillingNotConfiguredError,
    SubscriptionLimitError,
)


class WorkspaceAccess(BaseModel):
    """Container for workspace access information"""

    membership: UserWorkspaceInDb
    workspace: WorkspaceInDb
    user: UserInDb


async def require_workspace_member(
    workspace_id: str,
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> None:
    """Verify user is a member of workspace (any role)"""
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")


async def require_workspace_admin(
    workspace_id: str,
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
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
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
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
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
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
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
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
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
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
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> ComposeDeploymentInDb:
    """Get deployment and verify user has access (any role)"""
    deployment, role = await db.compose_deployments.get_with_workspace_access(
        deployment_id, current_user.id
    )

    if not deployment or not role:
        raise HTTPException(404, "Deployment not found")

    return deployment


async def get_deployment_with_admin_access(
    deployment_id: str,
    current_user: UserInDb = Depends(get_current_active_user),
    include_deleted: bool = False,
    db: Database = Depends(get_db),
) -> ComposeDeploymentInDb:
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
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> ComposeDeploymentInDb:
    """Get deployment with admin access, including deleted deployments for usage purposes"""
    return await get_deployment_with_admin_access(
        deployment_id, current_user, include_deleted=True, db=db
    )


async def get_deployment_with_active_subscription(
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_admin_access),
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> ComposeDeploymentInDb:
    """Get deployment with admin access and verify workspace owner has active subscription."""
    await get_owner_with_active_subscription(
        workspace_id=deployment.workspace_id,
        current_user=current_user,
        db=db,
    )
    return deployment


async def get_user_product_features(
    current_user: UserInDb = Depends(get_current_active_user),
) -> BaseFeatures:
    """Get product features for the current user based on their subscription.

    Admin users (role == ADMIN) get unlimited features regardless of subscription.
    """
    # Admin users get unlimited features
    if current_user.role == UserRole.ADMIN:
        return ADMIN_FEATURES

    subscription_service = get_subscription_service()
    try:
        return await subscription_service.get_user_features(
            external_customer_id=current_user.workos_id
        )

    except BillingNotConfiguredError:
        # Fallback when billing is disabled (e.g., local development)
        return DEVELOPER_FEATURES

    except ValueError as e:
        raise HTTPException(status_code=402, detail=str(e))

    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


async def get_owner_with_active_subscription(
    workspace_id: str,
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> UserInDb:
    """Get workspace owner after verifying access and active subscription.

    Verifies:
    1. Current user has access to the workspace
    2. Workspace owner has an active subscription (or is admin, or billing disabled)

    Returns the workspace owner for use in subsequent billing checks.
    Raises HTTP 402 if no active subscription.
    """
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")

    owner_user = await db.workspaces.get_owner_user(workspace_id)
    if not owner_user:
        raise HTTPException(500, "Workspace has no owner")

    # Admin workspace owners bypass subscription requirement
    if owner_user.role == UserRole.ADMIN:
        return owner_user

    # Check if Polar is enabled
    polar_service = get_polar_service()
    if not polar_service.enabled:
        return owner_user  # Allow through if billing is disabled

    subscription_service = get_subscription_service()
    try:
        await subscription_service.get_user_features(owner_user.workos_id)

    except NoActiveSubscriptionError:
        raise HTTPException(
            status_code=402,
            detail="Payment method required. Please add a payment method to continue.",
        )

    except (ValueError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail="Unable to verify subscription status. Please try again.",
        )

    return owner_user


async def get_features_for_owner(
    owner_user: UserInDb,
) -> BaseFeatures:
    """Get subscription features for a workspace owner.

    Admin users (role == ADMIN) get unlimited admin features.
    """
    if owner_user.role == UserRole.ADMIN:
        return ADMIN_FEATURES

    subscription_service = get_subscription_service()
    try:
        return await subscription_service.get_user_features(owner_user.workos_id)

    except BillingNotConfiguredError:
        # Fallback when billing is disabled (e.g., local development)
        return DEVELOPER_FEATURES

    except NoActiveSubscriptionError:
        raise HTTPException(
            status_code=402,
            detail="No active subscription. Please subscribe to access this feature.",
        )

    except (ValueError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail="Unable to verify subscription status. Please try again.",
        )


async def check_deployment_limit_for_owner(
    owner_user: UserInDb,
) -> None:
    """Check if workspace owner can create a new deployment based on their subscription tier.

    Use get_owner_with_active_subscription() first to get the owner, then pass here.
    Admin users (role == ADMIN) have no deployment limits.
    """
    if owner_user.role == UserRole.ADMIN:
        return

    subscription_service = get_subscription_service()
    try:
        owner_features = await subscription_service.get_user_features(
            owner_user.workos_id
        )
        await subscription_service.check_deployment_limit(owner_user.id, owner_features)

    except BillingNotConfiguredError:
        # Fallback when billing is disabled (e.g., local development)
        from backend.billing.product_details.base import DEVELOPER_FEATURES

        await subscription_service.check_deployment_limit(
            owner_user.id, DEVELOPER_FEATURES
        )

    except SubscriptionLimitError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

    except (ValueError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail="Unable to verify subscription status. Please try again.",
        )


async def check_deployment_features(
    compose_file: ComposeFile,
    features: BaseFeatures,
) -> None:
    """Check if deployment features (services, volumes, networks, domains) are within subscription limits."""
    subscription_service = get_subscription_service()
    try:
        # Apply tier-appropriate resource defaults before checking limits
        subscription_service.apply_tier_defaults(compose_file, features)

        # Check that the deployment is within limits
        await subscription_service.check_deployment_features(compose_file, features)

    except SubscriptionLimitError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
