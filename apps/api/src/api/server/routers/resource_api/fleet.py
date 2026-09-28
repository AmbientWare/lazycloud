from typing import Annotated
from uuid import UUID

from compute.fleet_status import FleetStatusService
from control.releases import DeploymentReleaseService
from fastapi import APIRouter, Depends, Query
from shared.http.fleet import FleetNodeListResponse, FleetSummaryResponse

from api.server.auth import admin_access
from api.server.dependencies import current_services
from api.server.service_dependencies import fleet_status_service
from api.server.services import ApiServices

router = APIRouter()


@router.get("/api/v1/fleet", response_model=FleetSummaryResponse, operation_id="get_fleet")
def get_fleet(
    _auth: admin_access,
    fleet: Annotated[FleetStatusService, Depends(fleet_status_service)],
    services: Annotated[ApiServices, Depends(current_services)],
) -> FleetSummaryResponse:
    return fleet.summary(
        DeploymentReleaseService().active(), services.scheduler_workers.list_workers()
    )


@router.get(
    "/api/v1/fleet/nodes", response_model=FleetNodeListResponse, operation_id="list_fleet_nodes"
)
def list_fleet_nodes(
    _auth: admin_access,
    fleet: Annotated[FleetStatusService, Depends(fleet_status_service)],
    services: Annotated[ApiServices, Depends(current_services)],
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> FleetNodeListResponse:
    return fleet.nodes(
        DeploymentReleaseService().active(),
        services.scheduler_workers.list_workers(),
        cursor=cursor,
        limit=limit,
    )
