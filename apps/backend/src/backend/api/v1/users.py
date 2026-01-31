from api_requests.users import OnboardingRequest
from fastapi import APIRouter, Depends, HTTPException
from responses.users import (
    CurrentUserResponse,
    OnboardingResponse,
    UserFeaturesResponse,
)

from backend.api.dependencies import get_user_product_features
from backend.api.security import get_current_active_user, require_admin
from backend.billing.product_details.features import BaseFeatures
from backend.database import Database, get_db
from backend.database.models import UserPydantic
from backend.services import get_user_onboarding_service
from backend.services.user_onboarding import UserOnboardingService

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/current")
async def current_user(
    current_user: UserPydantic = Depends(get_current_active_user),
) -> CurrentUserResponse:
    """Used to return the current user's id. This is typically used when user requests with an api key"""
    return CurrentUserResponse(id=current_user.id, workos_id=current_user.workos_id)


@users_router.get("/features")
async def get_user_features(
    current_user: UserPydantic = Depends(get_current_active_user),
    features: BaseFeatures = Depends(get_user_product_features),
    db: Database = Depends(get_db),
) -> UserFeaturesResponse:
    """Get the current user's subscription features and current usage counts."""
    # Get total deployment count across all workspaces
    total_deployments = (
        await db.compose_deployments.get_total_deployment_count_for_user(
            current_user.id
        )
    )

    return UserFeaturesResponse(
        deployment_limit=features.deployment_limit,
        deployment_count=total_deployments,
        max_team_members=features.max_team_members,
        max_cpu_per_service=features.max_cpu_per_service,
        max_memory_per_service=features.max_memory_per_service,
        max_replicas_per_service=features.max_replicas_per_service,
        custom_domains_enabled=features.custom_domains_enabled,
    )


@users_router.post("/onboarding")
async def onboard_user(
    request: OnboardingRequest,
    onboarding_service: UserOnboardingService = Depends(get_user_onboarding_service),
    _: UserPydantic = Depends(require_admin),
) -> OnboardingResponse:
    """Onboard a new user with proper transaction handling."""
    try:
        await onboarding_service.onboard_user(
            workos_id=request.workos_id,
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
