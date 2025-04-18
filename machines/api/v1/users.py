from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from machines.api.security import get_current_active_user, UserData, require_admin
from machines.database import db
from machines.database.usage import UsagePydantic
from machines.database.api_keys import ApiKeyPydantic, ApiKeyRole, ApiKeyExpirationDays
from machines.database.utils import generate_api_key, generate_api_key_expires_at
from datetime import datetime, timezone

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/id")
async def current_user(
    current_user: UserData = Depends(get_current_active_user),
) -> str:
    """Used to return the current user's id. This is typically used when user requests with an api key"""
    return current_user.user_id


class OnboardingRequest(BaseModel):
    user_id: str


class OnboardingResponse(BaseModel):
    success: bool


@users_router.post("/onboarding")
async def get_onboarding_status(
    request: OnboardingRequest,
    _: UserData = Depends(require_admin),
) -> OnboardingResponse:
    # check if the user has a usage record
    filters = {"user_id": request.user_id}
    usage = await db.usage.afind_one(filters=filters)
    api_key = await db.api_keys.afind_one(filters=filters)

    # if one exists, raise an error
    if usage is not None:
        raise HTTPException(status_code=400, detail="User already has a usage record")
    if api_key is not None:
        raise HTTPException(status_code=400, detail="User already has an api key")

    # create a new usage record
    usage = UsagePydantic(
        user_id=request.user_id,
        balance=0,
        last_collected_at=datetime.now(timezone.utc),
    )
    await db.usage.acreate(usage)

    # create a default api key that expires at the requested time
    api_key = ApiKeyPydantic(
        name="default",
        user_id=request.user_id,
        value=generate_api_key(),
        expires_at=generate_api_key_expires_at(ApiKeyExpirationDays.NEVER),
        role=ApiKeyRole.USER,
    )

    new_api_key = await db.api_keys.acreate(api_key)
    if new_api_key is None:
        raise HTTPException(status_code=404, detail="Unable to create api key")

    return OnboardingResponse(success=True)
