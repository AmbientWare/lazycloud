from __future__ import annotations

from fastapi import APIRouter, Depends
from shared.http.source_cache_cleanup import SourceCacheCleanupStatusResponse
from worker_repository.source_cache_status import SourceCacheCleanupStatusService

from api.server.auth import admin_access
from api.server.dependencies import current_services
from api.server.services import ApiServices

router = APIRouter()


@router.get(
    "/api/v1/workspaces/{workspace_id_or_name}/source-cache-cleanup",
    response_model=SourceCacheCleanupStatusResponse,
    operation_id="get_workspace_source_cache_cleanup_status",
)
def get_workspace_source_cache_cleanup_status(
    workspace_id_or_name: str,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> SourceCacheCleanupStatusResponse:
    snapshot = SourceCacheCleanupStatusService(services.context).get(workspace_id_or_name)
    return SourceCacheCleanupStatusResponse.model_validate(snapshot)


__all__ = ["router"]
