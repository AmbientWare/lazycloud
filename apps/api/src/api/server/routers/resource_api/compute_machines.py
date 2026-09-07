from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from gateway.machine_lifecycle import MachineLifecycleService
from gateway.service import GatewayControlService
from shared.compute_policy import MachinePool
from shared.http.compute import (
    MachineCreateRequest,
    MachineJoinCommandRequest,
    MachineJoinCommandResponse,
    MachineJoinStatusResponse,
    MachineJoinTokenResponse,
    MachineListResponse,
    MachineResponse,
    UnitMachineListResponse,
)

from api.server.auth import (
    read_user,
    read_workspace,
    write_token,
    write_user,
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
    "/api/v1/machines/self-hosted",
    response_model=UnitMachineListResponse,
    operation_id="list_self_hosted_machines",
)
def list_self_hosted_machines(
    user_id: read_user,
    pool: MachinePool = MachinePool(""),
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str = "",
    gateway: GatewayControlService = Depends(gateway_service),
) -> UnitMachineListResponse:
    """The account's joined machines, optionally narrowed to one pool.

    Account-scoped rather than workspace-scoped: a joined host belongs to the person
    who connected it and serves every workspace they own, so answering per workspace
    would hide their own hardware from them.
    """
    machines = [
        item
        for item in gateway.account_machine_views(user_id)
        if item.id > cursor and (not pool or item.pool == pool)
    ]
    selected = machines[:limit]
    return UnitMachineListResponse(
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
    user_id: write_user,
    service: GatewayControlService = Depends(gateway_service),
) -> MachineJoinCommandResponse:
    return service.machine_join_command(
        request,
        user_id=user_id,
        owner_token_id=token.id,
    )


@router.get(
    "/api/v1/machines/join-commands/{join_id}",
    response_model=MachineJoinStatusResponse,
    operation_id="get_machine_join_status",
)
def machine_join_status(
    join_id: UUID,
    user_id: read_user,
    service: GatewayControlService = Depends(gateway_service),
) -> MachineJoinStatusResponse:
    return service.machine_join_status(str(join_id), user_id=user_id)


@router.post(
    "/api/v1/machines/join-token",
    response_model=MachineJoinTokenResponse,
    operation_id="get_machine_join_token",
)
def machine_join_token(
    request: MachineJoinCommandRequest,
    token: write_token,
    user_id: write_user,
    service: GatewayControlService = Depends(gateway_service),
) -> MachineJoinTokenResponse:
    return service.machine_join_token(
        request,
        user_id=user_id,
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
