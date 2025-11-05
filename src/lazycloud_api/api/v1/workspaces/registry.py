from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.dependencies import (
    WorkspaceAccess,
    get_workspace_with_admin_access,
)
from lazycloud_api.services import ECRAuthService, get_ecr_auth_service
from shared.requests.registry import UploadIntentRequest
from shared.responses.registry import UploadIntentResponse

registry_router = APIRouter(prefix="/registry")


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
