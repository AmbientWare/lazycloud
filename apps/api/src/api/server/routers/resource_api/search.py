from __future__ import annotations

from control.search import ResourceSearchService
from fastapi import APIRouter, Depends, Query
from shared.http.search import ResourceSearchResponse

from api.server.auth import read_workspace
from api.server.dependencies import current_services
from api.server.services import ApiServices

router = APIRouter(tags=["search"])


def search_service(services: ApiServices = Depends(current_services)) -> ResourceSearchService:
    return ResourceSearchService(services.context)


@router.get(
    "/api/v1/search",
    response_model=ResourceSearchResponse,
    operation_id="search_workspace_resources",
)
def search_resources(
    workspace_id: read_workspace,
    q: str = Query(min_length=1, max_length=240),
    cursor: str = Query(default="", max_length=4096),
    limit: int = Query(default=30, ge=1, le=100),
    service: ResourceSearchService = Depends(search_service),
) -> ResourceSearchResponse:
    return service.search(workspace_id, q, cursor=cursor, limit=limit)
