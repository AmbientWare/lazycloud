from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from gateway.machine_lifecycle import MachineLifecycleService
from gateway.service import GatewayControlService
from shared.http.compute import (
    MachineJoinCommandRequest,
    MachineJoinCommandResponse,
    MachineListResponse,
    MachineResponse,
    MachineUpdateRequest,
    UnitMachineListResponse,
)

from api.server.auth import (
    read_user,
    read_workspace,
    write_token,
    write_user,
    write_workspace,
)
from api.server.service_dependencies import gateway_service, machine_lifecycle_service

router = APIRouter()


@router.get("/api/v1/machines", response_model=MachineListResponse, operation_id="list_machines")
def list_machines(
    workspace_id: read_workspace,
    gateway: GatewayControlService = Depends(gateway_service),
) -> MachineListResponse:
    return MachineListResponse(machines=gateway.machine_responses(workspace_id=workspace_id))


@router.get(
    "/api/v1/machines/self-hosted",
    response_model=UnitMachineListResponse,
    operation_id="list_self_hosted_machines",
)
def list_self_hosted_machines(
    user_id: read_user,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str = "",
    gateway: GatewayControlService = Depends(gateway_service),
) -> UnitMachineListResponse:
    """The account's joined machines.

    Account-scoped rather than workspace-scoped: a joined host belongs to the person
    who connected it, and which workspaces it serves is a property of the machine.
    """
    machines = [item for item in gateway.account_machine_views(user_id) if item.id > cursor]
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


@router.patch(
    "/api/v1/machines/{machine_id}",
    response_model=MachineResponse,
    operation_id="update_machine",
)
def update_machine(
    machine_id: str,
    request: MachineUpdateRequest,
    user_id: write_user,
    service: GatewayControlService = Depends(gateway_service),
) -> MachineResponse:
    return service.update_machine_workspaces(
        machine_id,
        user_id=user_id,
        workspace_names=request.workspaces,
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
