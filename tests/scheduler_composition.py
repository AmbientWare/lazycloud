from __future__ import annotations

from dataclasses import replace

from api.server.services import ApiServices
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from observability.stream_state import RedisEventStreamRepository
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.containers import (
    CONTAINER_DISPATCH_WAKE_SCOPE,
    SchedulerContainerRequestService,
)
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
)


def scheduler_request_service_for_redis(
    services: ApiServices,
    redis: RedisClient,
    *,
    workers: RedisSchedulerWorkerRepository | None = None,
    containers: RedisSchedulerContainerRepository | None = None,
) -> SchedulerContainerRequestService:
    scheduler = services.containers.scheduler
    if not isinstance(scheduler, SchedulerContainerRequestService):
        raise AssertionError("test services must use the production scheduler request service")
    return replace(
        scheduler,
        workers=workers or RedisSchedulerWorkerRepository(redis),
        containers=containers or RedisSchedulerContainerRepository(redis),
        dispatch_wake=RedisWakeSignal(redis, CONTAINER_DISPATCH_WAKE_SCOPE),
        lifecycle_events=RedisEventStreamRepository(redis),
        capacity_reservations=CapacityReservationService(
            RedisCapacityReservationRepository(redis),
            lambda: [],
        ),
    )


def services_with_redis_container_control(
    services: ApiServices,
    redis: RedisClient,
) -> ApiServices:
    return ApiServices.create(
        services.database,
        root=services.root,
        create_schema=False,
        workspace_storage_issuer=services.workspace_storage_issuer,
        tcp_ingress_settings=services.tcp_ingress_settings,
        agent_route_reconciliation_settings=services.agent_route_reconciliation_settings,
        gateway_settings=services.gateway_settings,
        workspace_change_stream_settings=services.workspace_change_stream_settings,
        agent_binary_settings=services.agent_binary_settings,
        aws_account_connection_settings=services.aws_account_connection_settings,
        aws_capacity_settings=services.aws_capacity_settings,
        aws_capacity_reconciliation_settings=services.aws_capacity_reconciliation_settings,
        object_store_settings=services.object_store_settings,
        object_storage=services.object_storage,
        image_build_registry_settings=services.image_build_registry_settings,
        container_service_settings=services.container_service_settings,
        volume_metering=services.volume_metering,
        redis_client=redis,
        binary_redis_client=redis,
        async_io=services.require_async_io(),
    )


__all__ = [
    "scheduler_request_service_for_redis",
    "services_with_redis_container_control",
]
