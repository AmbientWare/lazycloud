from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from gateway.service import GatewayControlService
from shared.compute_policy import ComputePoolRecord, MachinePool, UnitName
from shared.http.compute import (
    PoolCreateRequest,
    PoolJoinCommandRequest,
    PoolJoinCommandResponse,
    PoolJoinTokenRequest,
    PoolJoinTokenResponse,
    PoolListResponse,
    PoolMachineListResponse,
    PoolResponse,
    PoolScaleRequest,
    PoolScaleResponse,
)

from api.server.auth import admin_access, write_token
from api.server.dependencies import current_services, current_workspace_id
from api.server.service_dependencies import gateway_service
from api.server.services import ApiServices

router = APIRouter()


def _pool_state_response(pool: ComputePoolRecord) -> PoolScaleResponse:
    return PoolScaleResponse(
        name=pool.name,
        desired_machines=pool.desired_machines,
        max_machines=pool.max_machines,
        observed_machines=pool.observed_machines,
        phase=pool.phase,
        status=pool.status,
        degraded_reason=pool.provider_state.degraded_reason,
    )


@router.get("/api/v1/pools", response_model=PoolListResponse, operation_id="list_pools")
def list_pools(
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    services: ApiServices = Depends(current_services),
) -> PoolListResponse:
    return PoolListResponse(
        pools=[
            PoolResponse.model_validate(item)
            for item in services.compute.list_pools(workspace=workspace_id)
        ]
    )


@router.post(
    "/api/v1/pools",
    response_model=PoolResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_pool",
)
def create_pool(
    request: PoolCreateRequest,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    services: ApiServices = Depends(current_services),
) -> PoolResponse:
    return PoolResponse.model_validate(
        services.compute.create_pool(
            UnitName(request.name),
            workspace=workspace_id,
            machine_pool=MachinePool(request.machine_pool) if request.machine_pool else None,
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
    "/api/v1/pools/{pool_name}/scale",
    response_model=PoolScaleResponse,
    operation_id="scale_pool",
)
def scale_pool(
    pool_name: str,
    request: PoolScaleRequest,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> PoolScaleResponse:
    pool = gateway.scale_pool(
        pool_name,
        request.desired_machines,
        workspace_id=workspace_id,
    )
    return _pool_state_response(pool)


@router.get(
    "/api/v1/pools/{pool_name}/state",
    response_model=PoolScaleResponse,
    operation_id="get_pool_state",
)
def get_pool_state(
    pool_name: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> PoolScaleResponse:
    return _pool_state_response(gateway.pool_state(pool_name, workspace_id=workspace_id))


@router.post(
    "/api/v1/pools/{pool_name}/clear-degradation",
    response_model=PoolScaleResponse,
    operation_id="clear_pool_degradation",
)
def clear_pool_degradation(
    pool_name: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    services: ApiServices = Depends(current_services),
) -> PoolScaleResponse:
    return _pool_state_response(
        services.compute.clear_capacity_degradation(
            workspace=workspace_id,
            pool_name=pool_name,
        )
    )


@router.delete(
    "/api/v1/pools/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_pool",
)
def delete_pool(
    name: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> None:
    gateway.delete_pool(name, workspace_id=workspace_id)


@router.post(
    "/api/v1/pools/{pool_name}/join-token",
    response_model=PoolJoinTokenResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_pool_join_token",
)
def create_pool_join_token(
    pool_name: str,
    request: PoolJoinTokenRequest,
    _auth: admin_access,
    token: write_token,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> PoolJoinTokenResponse:
    plan = gateway.create_pool_join_token(
        pool_name,
        workspace_id=workspace_id,
        owner_token_id=token.id,
        ttl=request.ttl,
    )
    return PoolJoinTokenResponse(token=plan.token, expires_at=plan.expires_at)


@router.delete(
    "/api/v1/pools/{pool_name}/join-token",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="revoke_pool_join_token",
)
def revoke_pool_join_token(
    pool_name: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> None:
    gateway.revoke_pool_join_token(pool_name, workspace_id=workspace_id)


@router.post(
    "/api/v1/pools/{pool_name}/join-command",
    response_model=PoolJoinCommandResponse,
    operation_id="get_pool_join_command",
)
def get_pool_join_command(
    pool_name: str,
    request: PoolJoinCommandRequest,
    _auth: admin_access,
    token: write_token,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    gateway: GatewayControlService = Depends(gateway_service),
) -> PoolJoinCommandResponse:
    return gateway.pool_join_command(
        pool_name,
        workspace_id=workspace_id,
        owner_token_id=token.id,
        ttl=request.ttl,
    )


@router.get(
    "/api/v1/pools/{pool_name}/machines",
    response_model=PoolMachineListResponse,
    operation_id="list_pool_machines",
)
def list_pool_machines(
    pool_name: str,
    _auth: admin_access,
    workspace_id: Annotated[str, Depends(current_workspace_id)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str = "",
    gateway: GatewayControlService = Depends(gateway_service),
) -> PoolMachineListResponse:
    return gateway.pool_machine_views(
        pool_name,
        workspace_id=workspace_id,
        limit=limit,
        cursor=cursor,
    )
