from fastapi import APIRouter, Depends, HTTPException

from lazycloud_api.api.security import get_current_active_user, require_admin
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.services import get_user_onboarding_service
from lazycloud_api.services.user_onboarding import UserOnboardingService
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
    onboarding_service: UserOnboardingService = Depends(get_user_onboarding_service),
    _: UserPydantic = Depends(require_admin),
) -> OnboardingResponse:
    """Onboard a new user with proper transaction handling."""
    try:
        await onboarding_service.onboard_user(
            clerk_id=request.clerk_id,
            name=request.name,
            email=request.email,
        )
        return OnboardingResponse(success=True)

    except ValueError as e:
        # User already exists or validation error
        raise HTTPException(status_code=400, detail=str(e))

    except Exception:
        # Unexpected error
        raise HTTPException(status_code=500, detail="Internal server error")
