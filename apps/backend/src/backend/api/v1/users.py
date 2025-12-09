from api_requests.users import OnboardingRequest
from fastapi import APIRouter, Depends, HTTPException
from responses.users import (
    CurrentUserResponse,
    DeploymentFeatureResponse,
    OnboardingResponse,
    UserFeaturesResponse,
    WorkspaceFeatureResponse,
)

from backend.api.dependencies import get_user_product_features
from backend.api.security import get_current_active_user, require_admin
from backend.billing.product_details.features import BaseFeatures
from backend.database import Database, get_db
from backend.database.users import UserPydantic
from backend.services import get_user_onboarding_service
from backend.services.user_onboarding import UserOnboardingService

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/current")
async def current_user(
    current_user: UserPydantic = Depends(get_current_active_user),
) -> CurrentUserResponse:
    """Used to return the current user's id. This is typically used when user requests with an api key"""
    return current_user


@users_router.get("/features")
async def get_user_features(
    current_user: UserPydantic = Depends(get_current_active_user),
    features: BaseFeatures = Depends(get_user_product_features),
    db: Database = Depends(get_db),
) -> UserFeaturesResponse:
    """Get the current user's subscription features and current usage counts."""
    workspace_count = await db.workspaces.get_active_workspace_count(current_user.id)

    return UserFeaturesResponse(
        workspace=WorkspaceFeatureResponse(
            limit=features.workspace.limit,
            deployment_limit=features.workspace.deployment_limit,
            current_count=workspace_count,
        ),
        deployment=DeploymentFeatureResponse(
            service_limit=features.deployment.service_limit,
            volume_limit=features.deployment.volume_limit,
            network_limit=features.deployment.network_limit,
        ),
        domain_limit=features.domain_limit,
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
