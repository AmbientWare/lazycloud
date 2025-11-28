from api_requests.registry import ImageExistsRequest, UploadIntentRequest
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from responses.registry import ImageExistsResponse, UploadIntentResponse

from backend.api.dependencies import (
    WorkspaceAccess,
    get_workspace_with_admin_access,
)
from backend.services import ECRAuthService, get_ecr_auth_service

registry_router = APIRouter(prefix="/{workspace_id}/registry")


@registry_router.post("/upload-intent", response_model=UploadIntentResponse)
async def get_upload_intent(
    request: UploadIntentRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
    ecr_auth_service: ECRAuthService = Depends(get_ecr_auth_service),
) -> UploadIntentResponse:
    """Get temporary ECR push credentials for a deployment"""

    workspace = workspace_access.workspace

    try:
        logger.info(
            f"Getting upload credentials for lc-user-{workspace_access.user.id}/deployment-{request.deployment_name}"
        )

        credentials = await ecr_auth_service.get_upload_credentials(
            workspace_id=workspace.id,
            deployment_name=request.deployment_name,
            repo_name=request.repo_name,
            session_name=request.session_name,  # Optional, will be auto-generated if None
        )

        return UploadIntentResponse(
            registry_url=credentials.registry_url,
            username=credentials.username,
            password=credentials.password,
            repository=credentials.repository,
            expires_at=credentials.expires_at,
        )

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        raise HTTPException(status_code=500, detail="ECR service not configured")

    except Exception as e:
        logger.error(f"Failed to get upload credentials: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@registry_router.post("/images-exist", response_model=ImageExistsResponse)
async def check_images_exist(
    request: ImageExistsRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
    ecr_auth_service: ECRAuthService = Depends(get_ecr_auth_service),
) -> ImageExistsResponse:
    """Check if images exist in the registry."""

    workspace = workspace_access.workspace

    try:
        logger.debug(
            f"Checking existence for {len(request.image_names)} images in deployment {request.deployment_name}"
        )

        exists_map = await ecr_auth_service.check_images_exist(
            workspace_id=workspace.id,
            deployment_name=request.deployment_name,
            image_names=request.image_names,
        )

        return ImageExistsResponse(exists_map=exists_map)

    except Exception as e:
        logger.error(f"Failed to check image existence: {e}")
        # On error, return False for all (will trigger builds)
        return ImageExistsResponse(
            exists_map={img: False for img in request.image_names}
        )
