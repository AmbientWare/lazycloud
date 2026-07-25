from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from gateway.machine_lifecycle import MachineLifecycleService
from gateway.service import SELF_HOSTED_FLEET_POOL_NAME, GatewayControlService
from shared.http.compute import (
    MachineConfigResponse,
    MachineCreateRequest,
    MachineGpuCountsResponse,
    MachineJoinCommandRequest,
    MachineJoinCommandResponse,
    MachineListResponse,
    MachineRegisterRequest,
    MachineRegisterResponse,
    MachineRemoteConfigResponse,
    MachineResponse,
    PoolMachineListResponse,
)

from api.server.auth import machine_access, read_workspace, write_token, write_workspace
from api.server.dependencies import current_services
from api.server.routers.resource_api.common import _management
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
    "/api/v1/machines/self-hosted",
    response_model=PoolMachineListResponse,
    operation_id="list_self_hosted_machines",
)
def list_self_hosted_machines(
    workspace_id: read_workspace,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str = "",
    gateway: GatewayControlService = Depends(gateway_service),
) -> PoolMachineListResponse:
    machines = sorted(
        (
            item
            for item in gateway.machine_views(workspace_id)
            if item.pool_name == SELF_HOSTED_FLEET_POOL_NAME and item.id > cursor
        ),
        key=lambda item: item.id,
    )
    selected = machines[:limit]
    return PoolMachineListResponse(
        data=selected,
        next=selected[-1].id if len(machines) > limit else "",
    )


@router.get(
    "/api/v1/machines/gpus",
    response_model=MachineGpuCountsResponse,
    operation_id="get_machine_gpu_counts",
)
def api_v1_gpu_counts(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> MachineGpuCountsResponse:
    return MachineGpuCountsResponse(gpus=_management(services).gpu_counts(workspace_id))


@router.post(
    "/api/v1/machines/register",
    response_model=MachineRegisterResponse,
    operation_id="register_machine",
)
def api_v1_register_machine(
    request: MachineRegisterRequest,
    _auth: machine_access,
    services: ApiServices = Depends(current_services),
) -> MachineRegisterResponse:
    machine = _management(services).register_machine(
        machine_id=request.machine_id,
        provider_name=request.provider_name,
        pool_name=request.pool_name,
        hostname=request.hostname,
        cpu=request.cpu,
        memory=request.memory,
        gpu_count=request.gpu_count,
        private_ip=request.private_ip,
        token=request.token,
    )
    return MachineRegisterResponse(
        machine=MachineResponse.model_validate(machine),
        config=MachineRemoteConfigResponse.model_validate(_management(services).machine_config()),
    )


@router.get(
    "/api/v1/machines/config",
    response_model=MachineConfigResponse,
    operation_id="get_machine_config",
)
def api_v1_machine_config(
    _auth: machine_access,
    services: ApiServices = Depends(current_services),
) -> MachineConfigResponse:
    return MachineConfigResponse(
        config=MachineRemoteConfigResponse.model_validate(_management(services).machine_config())
    )


@router.get(
    "/api/v1/machines/list",
    response_model=MachineListResponse,
    operation_id="list_ready_machines",
)
def api_v1_list_pool_machines(
    provider_name: str | None = None,
    pool_name: str | None = None,
    *,
    _auth: machine_access,
    services: ApiServices = Depends(current_services),
) -> MachineListResponse:
    return MachineListResponse(
        machines=[
            MachineResponse.model_validate(item)
            for item in _management(services).list_ready_machines(
                provider_name=provider_name,
                pool_name=pool_name,
            )
        ]
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
            pool=request.pool,
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
