from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from responses.builds import DepotTokenResponse

from backend.api.dependencies import (
    WorkspaceAccess,
    get_workspace_with_admin_access,
)
from backend.config import app_config
from backend.database import Database, get_db
from backend.services import (
    DepotService,
    get_depot_service,
)

builds_router = APIRouter(prefix="/{workspace_id}/builds")


@builds_router.post("/token", response_model=DepotTokenResponse)
async def get_depot_token(
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
    depot_service: DepotService = Depends(get_depot_service),
    deployment_name: str = Query(..., description="Deployment name for the build"),
    db: Database = Depends(get_db),
) -> DepotTokenResponse:
    """Get Depot project token for CLI builds.

    Returns a project token that the CLI can use to run `depot build`.
    The token is scoped to the deployment's Depot project.
    Images are stored in Depot's registry (registry.depot.dev).
    """
    workspace = workspace_access.workspace

    if not depot_service.is_configured:
        raise HTTPException(
            status_code=503,
            detail="Remote builds are not configured. Contact support to enable Depot builds.",
        )

    try:
        # Look up deployment to get its ID
        deployment = await db.compose_deployments.get_by_name(
            workspace_id=str(workspace.id), name=deployment_name
        )
        if not deployment:
            raise HTTPException(
                status_code=404,
                detail=f"Deployment '{deployment_name}' not found in workspace",
            )

        logger.info(
            f"Getting Depot build token for deployment {deployment_name} (ID: {deployment.id}) in workspace {workspace.id}"
        )

        # Get or create project and generate token (cached in Redis with TTL)
        credentials = await depot_service.get_build_token(
            deployment=deployment,
            registry_url=app_config.DEPOT_REGISTRY_URL,
        )

        return DepotTokenResponse(
            project_id=credentials.project_id,
            token=credentials.token,
            expires_at=credentials.expires_at,
            registry_url=app_config.DEPOT_REGISTRY_URL,
        )

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        raise HTTPException(status_code=503, detail=str(e))

    except Exception as e:
        logger.error("Failed to get Depot token: {}", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get build token: {e}")
