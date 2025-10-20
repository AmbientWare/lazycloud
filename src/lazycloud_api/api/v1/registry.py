from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.services import ecr_auth_service
from shared.requests.registry import UploadIntentRequest
from shared.responses.registry import UploadIntentResponse

router = APIRouter(prefix="/registry", tags=["registry"])


@router.post("/upload-intent", response_model=UploadIntentResponse)
async def get_upload_intent(
    request: UploadIntentRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> UploadIntentResponse:
    """Get temporary ECR push credentials for a deployment"""

    try:
        logger.info(
            f"Getting upload credentials for lc-user-{current_user.id}/deployment-{request.deployment_name}"
        )

        credentials = await ecr_auth_service.get_upload_credentials(
            workspace_id=request.workspace_id,
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
