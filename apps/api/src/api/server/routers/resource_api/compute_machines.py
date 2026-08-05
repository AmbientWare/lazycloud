from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from gateway.machine_lifecycle import MachineLifecycleService
from gateway.service import SELF_HOSTED_FLEET_POOL_NAME, GatewayControlService
from shared.http.compute import (
    MachineCreateRequest,
    MachineJoinCommandRequest,
    MachineJoinCommandResponse,
    MachineListResponse,
    MachineResponse,
    PoolMachineListResponse,
)

from api.server.auth import (
    read_workspace,
    write_token,
    write_workspace,
)
from api.server.dependencies import current_services
from api.server.service_dependencies import gateway_service, machine_lifecycle_service
from api.server.services import ApiServices

router = APIRouter()


@router.get("/api/v1/machines", response_model=MachineListResponse, operation_id="list_machines")
def list_machines(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> MachineListResponse:
    return MachineListResponse(
        machines=[
            MachineResponse.model_validate(item)
            for item in services.compute.list_machines(workspace=workspace_id)
        ]
    )


@router.get(
    "/api/v1/machines/pool",
    response_model=PoolMachineListResponse,
    operation_id="list_pool_machines_by_group",
)
def list_machines_in_pool(
    workspace_id: read_workspace,
    pool: str = SELF_HOSTED_FLEET_POOL_NAME,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str = "",
    gateway: GatewayControlService = Depends(gateway_service),
) -> PoolMachineListResponse:
    """Machines in one pool, self-hosted by default."""
    machines = sorted(
        (
            item
            for item in gateway.machine_views(workspace_id)
            if item.pool_name == pool and item.id > cursor
        ),
        key=lambda item: item.id,
    )
    selected = machines[:limit]
    return PoolMachineListResponse(
        data=selected,
        next=selected[-1].id if len(machines) > limit else "",
    )


@router.post(
    "/api/v1/machines/join-command",
    response_model=MachineJoinCommandResponse,
    operation_id="get_machine_join_command",
)
def machine_join_command(
    request: MachineJoinCommandRequest,
    token: write_token,
    workspace_id: write_workspace,
    service: GatewayControlService = Depends(gateway_service),
) -> MachineJoinCommandResponse:
    return service.machine_join_command(
        request,
        workspace_id=workspace_id,
        owner_token_id=token.id,
    )


@router.post(
    "/api/v1/machines",
    response_model=MachineResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_machine",
)
def create_machine(
    request: MachineCreateRequest,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> MachineResponse:
    return MachineResponse.model_validate(
        services.compute.create_machine(
            workspace=workspace_id,
            pool=services.workspace_compute_policy_service.default_machine_pool(
                workspace=workspace_id
            ),
            provider=request.provider,
            cpu=request.cpu,
            memory=request.memory,
            gpu=request.gpu,
            address=request.address,
            labels=request.labels,
        )
    )


@router.delete(
    "/api/v1/machines/{machine_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_machine",
)
def delete_machine(
    machine_id: str,
    workspace_id: write_workspace,
    lifecycle: MachineLifecycleService = Depends(machine_lifecycle_service),
) -> None:
    lifecycle.delete_machine(machine_id, workspace_id=workspace_id)
