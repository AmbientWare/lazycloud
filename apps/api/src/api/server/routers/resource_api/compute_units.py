from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from gateway.service import GatewayControlService
from shared.compute_policy import ComputeUnitRecord, MachinePool, UnitName
from shared.http.compute import (
    UnitCreateRequest,
    UnitJoinCommandRequest,
    UnitJoinCommandResponse,
    UnitJoinTokenRequest,
    UnitJoinTokenResponse,
    UnitListResponse,
    UnitMachineListResponse,
    UnitResponse,
    UnitScaleRequest,
    UnitScaleResponse,
)

from api.server.auth import admin_access, write_token
from api.server.dependencies import current_services, current_workspace_id
from api.server.service_dependencies import gateway_service
from api.server.services import ApiServices

router = APIRouter()


def _unit_state_response(pool: ComputeUnitRecord) -> UnitScaleResponse:
    return UnitScaleResponse(
        id=pool.id,
        name=pool.name,
        desired_machines=pool.desired_machines,
        max_machines=pool.max_machines,
        observed_machines=pool.observed_machines,
        phase=pool.phase,
        status=pool.status,
        degraded_reason=pool.provider_state.degraded_reason,
    )


@router.get("/api/v1/units", response_model=UnitListResponse, operation_id="list_units")
def list_units(
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    services: ApiServices = Depends(current_services),
) -> UnitListResponse:
    return UnitListResponse(
        pools=[
            UnitResponse.model_validate(item)
            for item in services.compute.list_units(workspace=workspace_id)
        ]
    )


@router.post(
    "/api/v1/units",
    response_model=UnitResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_unit",
)
def create_unit(
    request: UnitCreateRequest,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    services: ApiServices = Depends(current_services),
) -> UnitResponse:
    return UnitResponse.model_validate(
        services.compute.create_unit(
            UnitName(request.name),
            workspace=workspace_id,
            pool=MachinePool(request.pool) if request.pool else None,
            provider=request.provider,
            initial_machines=request.initial_machines,
            min_machines=request.min_machines,
            max_machines=request.max_machines,
            scaling_enabled=request.scaling_enabled,
            default_eligible=request.default_eligible,
            priority=request.priority,
            min_free_cpu_millicores=request.min_free_cpu_millicores,
            min_free_memory_mib=request.min_free_memory_mib,
            min_free_gpu_count=request.min_free_gpu_count,
            worker_cpu_millicores=request.worker_cpu_millicores,
            worker_memory_mib=request.worker_memory_mib,
            worker_gpu_type=request.worker_gpu_type,
            worker_gpu_count=request.worker_gpu_count,
            worker_runtimes=request.worker_runtimes,
            worker_preemptible=request.worker_preemptible,
            idle_drain_timeout_seconds=request.idle_drain_timeout_seconds,
            scale_up_cooldown_seconds=request.scale_up_cooldown_seconds,
            scale_down_cooldown_seconds=request.scale_down_cooldown_seconds,
            registration_timeout_seconds=request.registration_timeout_seconds,
        )
    )


@router.put(
    "/api/v1/units/{unit_id}/scale",
    response_model=UnitScaleResponse,
    operation_id="scale_unit",
)
def scale_unit(
    unit_id: str,
    request: UnitScaleRequest,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> UnitScaleResponse:
    pool = gateway.scale_unit(
        unit_id,
        request.desired_machines,
        workspace_id=workspace_id,
    )
    return _unit_state_response(pool)


@router.get(
    "/api/v1/units/{unit_id}/state",
    response_model=UnitScaleResponse,
    operation_id="get_unit_state",
)
def get_unit_state(
    unit_id: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> UnitScaleResponse:
    return _unit_state_response(gateway.unit_state(unit_id, workspace_id=workspace_id))


@router.post(
    "/api/v1/units/{unit_id}/clear-degradation",
    response_model=UnitScaleResponse,
    operation_id="clear_unit_degradation",
)
def clear_unit_degradation(
    unit_id: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> UnitScaleResponse:
    return _unit_state_response(gateway.clear_unit_degradation(unit_id, workspace_id=workspace_id))


@router.delete(
    "/api/v1/units/{unit_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_unit",
)
def delete_unit(
    unit_id: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> None:
    gateway.delete_unit(unit_id, workspace_id=workspace_id)


@router.post(
    "/api/v1/units/{unit_id}/join-token",
    response_model=UnitJoinTokenResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_unit_join_token",
)
def create_unit_join_token(
    unit_id: str,
    request: UnitJoinTokenRequest,
    _auth: admin_access,
    token: write_token,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> UnitJoinTokenResponse:
    plan = gateway.create_unit_join_token(
        unit_id,
        workspace_id=workspace_id,
        owner_token_id=token.id,
        ttl=request.ttl,
    )
    return UnitJoinTokenResponse(token=plan.token, expires_at=plan.expires_at)


@router.delete(
    "/api/v1/units/{unit_id}/join-token",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="revoke_unit_join_token",
)
def revoke_unit_join_token(
    unit_id: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> None:
    gateway.revoke_unit_join_token(unit_id, workspace_id=workspace_id)


@router.post(
    "/api/v1/units/{unit_id}/join-command",
    response_model=UnitJoinCommandResponse,
    operation_id="get_unit_join_command",
)
def get_unit_join_command(
    unit_id: str,
    request: UnitJoinCommandRequest,
    _auth: admin_access,
    token: write_token,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> UnitJoinCommandResponse:
    return gateway.unit_join_command(
        unit_id,
        workspace_id=workspace_id,
        owner_token_id=token.id,
        ttl=request.ttl,
    )


@router.get(
    "/api/v1/units/{unit_id}/machines",
    response_model=UnitMachineListResponse,
    operation_id="list_unit_machines",
)
def list_unit_machines(
    unit_id: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str = "",
    gateway: GatewayControlService = Depends(gateway_service),
) -> UnitMachineListResponse:
    return gateway.unit_machine_views(
        unit_id,
        workspace_id=workspace_id,
        limit=limit,
        cursor=cursor,
    )
