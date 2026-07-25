from __future__ import annotations

from typing import Annotated

from compute.offers import ComputeOffer
from compute.projection import PoolConfig as ComputePoolConfig
from compute.projection import PrivatePoolState
from fastapi import APIRouter, Depends, Query, Response, status
from gateway.service import GatewayControlService
from identity.auth import AuthorizationDeniedError
from shared.compute_policy import ComputePoolRecord
from shared.http.compute import (
    PoolCapacityExtendRequest,
    PoolCapacityLaunchRequest,
    PoolCapacityResponse,
    PoolCreateRequest,
    PoolJoinCommandRequest,
    PoolJoinCommandResponse,
    PoolJoinTokenRequest,
    PoolJoinTokenResponse,
    PoolListResponse,
    PoolMachineListResponse,
    PoolOfferListResponse,
    PoolOfferQuery,
    PoolOfferResponse,
    PoolProviderInstanceResponse,
    PoolResponse,
    PoolScaleRequest,
    PoolScaleResponse,
)
from shared.identity import TokenKind

from api.server.auth import read_workspace, write_token, write_workspace
from api.server.dependencies import current_services
from api.server.service_dependencies import gateway_service
from api.server.services import ApiServices

router = APIRouter()


def _compute_pool_config(pool_name: str, request: PoolOfferQuery) -> ComputePoolConfig:
    return ComputePoolConfig(
        name=pool_name,
        providers=request.provider,
        regions=request.region,
        gpu=request.gpu,
        nodes=request.node_count,
        ttl=request.ttl,
        max_spend=request.max_spend,
        min_reliability=request.min_reliability,
        offer_id=request.offer_id,
    )


def _offer_response(offer: ComputeOffer) -> PoolOfferResponse:
    return PoolOfferResponse(
        id=offer.id,
        provider=offer.provider,
        instance_type=offer.instance_type,
        region=offer.region,
        gpu=offer.gpu or "",
        gpu_count=offer.gpu_count,
        cpu_millicores=offer.cpu_millicores,
        memory_mb=offer.memory_mb,
        hourly_cost_micros=offer.hourly_cost_micros,
        reliability=offer.reliability,
        available=offer.available,
        storage_mb=offer.storage_mb,
        cloud=offer.cloud,
        node_count=offer.node_count,
        display_name=offer.display_name,
        category=offer.category,
        region_display_name=offer.region_display_name,
        latitude=offer.latitude,
        longitude=offer.longitude,
    )


def _capacity_response(state: PrivatePoolState) -> PoolCapacityResponse:
    config = state.config or ComputePoolConfig(name=state.name)
    return PoolCapacityResponse(
        name=state.name,
        selector=state.selector,
        reservations=[
            PoolProviderInstanceResponse(
                id=item.id,
                provider=item.provider,
                offer_id=item.offer_id,
                status=item.status,
                gpu_count=item.gpu_count,
                hourly_cost_micros=item.hourly_cost_micros,
                created_at=item.created_at,
                expires_at=item.expires_at,
                machine_id=item.machine_id,
                region=item.region,
                node_count=item.node_count,
                instance_type=item.instance_type,
            )
            for item in state.reservations
        ],
        committed_spend_micros=state.committed_spend_micros,
        max_spend_micros=int(config.max_spend * 1_000_000),
        status=state.status,
        expires_at=state.expires_at,
        reserved_nodes=state.reserved_nodes,
    )


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


def _pool_query(
    provider: Annotated[list[str] | None, Query()] = None,
    region: Annotated[list[str] | None, Query()] = None,
    gpu: Annotated[list[str] | None, Query()] = None,
    node_count: Annotated[int, Query(ge=1)] = 1,
    ttl: str = "",
    max_spend: Annotated[float, Query(ge=0.0)] = 0.0,
    min_reliability: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
    offer_id: str = "",
) -> PoolOfferQuery:
    return PoolOfferQuery(
        provider=provider or [],
        region=region or [],
        gpu=gpu or [],
        node_count=node_count,
        ttl=ttl,
        max_spend=max_spend,
        min_reliability=min_reliability,
        offer_id=offer_id,
    )


@router.get("/api/v1/pools", response_model=PoolListResponse, operation_id="list_pools")
def list_pools(
    workspace_id: read_workspace,
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
    token: write_token,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> PoolResponse:
    if request.provider.strip().lower() == "kubernetes" and token.kind is not TokenKind.Admin:
        raise AuthorizationDeniedError("admin token required to create Kubernetes capacity pools")
    return PoolResponse.model_validate(
        services.compute.create_pool(
            request.name,
            workspace=workspace_id,
            provider=request.provider,
            initial_workers=request.initial_workers,
            min_workers=request.min_workers,
            max_workers=request.max_workers,
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
            labels=request.labels,
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
    workspace_id: write_workspace,
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
    workspace_id: read_workspace,
    gateway: GatewayControlService = Depends(gateway_service),
) -> PoolScaleResponse:
    return _pool_state_response(gateway.pool_state(pool_name, workspace_id=workspace_id))


@router.delete(
    "/api/v1/pools/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_pool",
)
def delete_pool(
    name: str,
    workspace_id: write_workspace,
    gateway: GatewayControlService = Depends(gateway_service),
) -> None:
    gateway.delete_pool(name, workspace_id=workspace_id)


@router.get(
    "/api/v1/pools/{pool_name}/offers",
    response_model=PoolOfferListResponse,
    operation_id="list_pool_offers",
)
def list_pool_offers(
    pool_name: str,
    workspace_id: read_workspace,
    request: Annotated[PoolOfferQuery, Depends(_pool_query)],
    services: ApiServices = Depends(current_services),
) -> PoolOfferListResponse:
    offers = services.compute.list_pool_offers(
        _compute_pool_config(pool_name, request),
        workspace=workspace_id,
    )
    return PoolOfferListResponse(data=[_offer_response(item) for item in offers])


@router.post(
    "/api/v1/pools/{pool_name}/capacity",
    response_model=PoolCapacityResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="launch_pool_capacity",
)
def launch_pool_capacity(
    pool_name: str,
    request: PoolCapacityLaunchRequest,
    token: write_token,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> PoolCapacityResponse:
    state = services.compute.launch_pool_capacity(
        _compute_pool_config(pool_name, request),
        workspace=workspace_id,
        nodes=request.node_count,
        owner_token_id=token.id,
    )
    return _capacity_response(state)


@router.patch(
    "/api/v1/pools/{pool_name}/capacity",
    response_model=PoolCapacityResponse,
    operation_id="extend_pool_capacity",
)
def extend_pool_capacity(
    pool_name: str,
    request: PoolCapacityExtendRequest,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> PoolCapacityResponse:
    state = services.compute.extend_pool_capacity(
        pool_name,
        workspace=workspace_id,
        ttl=request.ttl,
        max_spend=request.max_spend,
    )
    return _capacity_response(state)


@router.post(
    "/api/v1/pools/{pool_name}/join-token",
    response_model=PoolJoinTokenResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_pool_join_token",
)
def create_pool_join_token(
    pool_name: str,
    request: PoolJoinTokenRequest,
    token: write_token,
    workspace_id: write_workspace,
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
    workspace_id: write_workspace,
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
    token: write_token,
    workspace_id: write_workspace,
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
    workspace_id: read_workspace,
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
