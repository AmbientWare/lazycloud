from fastapi import APIRouter, Depends, HTTPException

from lazycloud_api.api.security import get_current_active_user, require_admin
from lazycloud_api.database import db
from lazycloud_api.database.api_keys import (
    ApiKeyExpirationDays,
    ApiKeyPydantic,
)
from lazycloud_api.database.user_workspaces import (
    UserWorkspacePydantic,
    UserWorkspaceStatus,
    WorkspaceRole,
)
from lazycloud_api.database.users import (
    UserPydantic,
    UserRole,
    UserStatus,
)
from lazycloud_api.database.utils import generate_api_key, generate_api_key_expires_at
from lazycloud_api.database.workspaces import WorkspacePydantic
from shared.requests.users import OnboardingRequest
from shared.responses.users import CurrentUserResponse, OnboardingResponse

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/current")
async def current_user(
    current_user: UserPydantic = Depends(get_current_active_user),
) -> CurrentUserResponse:
    """Used to return the current user's id. This is typically used when user requests with an api key"""
    return current_user


@users_router.post("/onboarding")
async def onboard_user(
    request: OnboardingRequest,
    _: UserPydantic = Depends(require_admin),
) -> OnboardingResponse:
    # check if the user reference exists
    user = await db.users.aget_by_clerk_id(clerk_id=request.user_id)

    # if user already exists, raise an error
    if user:
        raise HTTPException(status_code=400, detail="User already onboarded")

    new_api_key: ApiKeyPydantic | None = None
    user_personal_workspace: WorkspacePydantic | None = None
    user_workspace_membership: UserWorkspacePydantic | None = None
    try:
        # create a new user reference
        user = UserPydantic(
            clerk_id=request.user_id,
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        user = await db.users.acreate(user)

        # create a default api key that expires at the requested time
        api_key = ApiKeyPydantic(
            name="default",
            user_id=user.id,
            value=generate_api_key(),
            expires_at=generate_api_key_expires_at(ApiKeyExpirationDays.NEVER),
        )

        new_api_key = await db.api_keys.acreate(api_key)

        # create a personal workspace for the user
        user_personal_workspace = WorkspacePydantic(
            name="Personal",
            is_personal=True,
        )
        user_personal_workspace = await db.workspaces.acreate(user_personal_workspace)

        # link user to their personal workspace as owner
        user_workspace_membership = UserWorkspacePydantic(
            user_id=user.id,
            workspace_id=user_personal_workspace.id,
            role=WorkspaceRole.OWNER,
            status=UserWorkspaceStatus.ACTIVE,
        )
        user_workspace_membership = await db.user_workspaces.acreate(
            user_workspace_membership
        )

    except Exception as e:
        # if any of the operations fail, delete the created objects (in reverse order)
        if user_workspace_membership:
            await db.user_workspaces.adelete(user_workspace_membership.id)
        if user_personal_workspace:
            await db.workspaces.adelete(user_personal_workspace.id)
        if new_api_key:
            await db.api_keys.adelete(new_api_key.id)
        if user:
            await db.users.adelete(user.id)

        raise HTTPException(status_code=500, detail=str(e))

    return OnboardingResponse(success=True)
