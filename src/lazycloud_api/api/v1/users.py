from fastapi import APIRouter, Depends, HTTPException

from lazycloud_api.api.security import get_current_active_user, require_admin
from lazycloud_api.database import db
from lazycloud_api.database.api_keys import (
    ApiKeyExpirationDays,
    ApiKeyPydantic,
)
from lazycloud_api.database.users import (
    UserPydantic,
    UserRole,
    UserStatus,
)
from lazycloud_api.database.utils import generate_api_key, generate_api_key_expires_at
from shared.requests.users import OnboardingRequest
from shared.responses.users import OnboardingResponse

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/id")
async def current_user(
    current_user: UserPydantic = Depends(get_current_active_user),
) -> str:
    """Used to return the current user's id. This is typically used when user requests with an api key"""
    return current_user.id


@users_router.post("/onboarding")
async def get_onboarding_status(
    request: OnboardingRequest,
    _: UserPydantic = Depends(require_admin),
) -> OnboardingResponse:
    # check if the user reference exists
    user = await db.users.aget_by_clerk_id(clerk_id=request.user_id)

    # if user already exists, raise an error
    if user:
        raise HTTPException(status_code=400, detail="User already onboarded")

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
    if new_api_key is None:
        raise HTTPException(status_code=404, detail="Unable to create api key")

    return OnboardingResponse(success=True)
