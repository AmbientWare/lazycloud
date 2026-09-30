from __future__ import annotations

from dataclasses import dataclass, replace
from types import TracebackType

from compute.capacity_recovery import CAPACITY_WAKE_SCOPE
from compute.state import RedisComputeStateRepository
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from execution.containers.preemption import PreemptedContainerService
from identity.token_invalidation import AuthTokenInvalidation, configure_token_invalidation
from scheduler.adapters import (
    DatabaseCapacityAllocationOwners,
    DatabaseMachineContainers,
)
from scheduler.agent_pool import SchedulerAgentPoolService
from scheduler.capacity_controls import SchedulerCapacityControllerProvider
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.containers import SchedulerContainerRequestService
from scheduler.orphan_recovery import OrphanedContainerRecovery
from scheduler.pool_drain import WorkerPoolDrainService
from scheduler.pool_state import SchedulerPoolStateService
from scheduler.preemption import (
    SchedulerCapacityInterruptionService,
    SchedulerWorkerMaintenanceService,
    SchedulerWorkerPreemptionService,
)
from scheduler.reconciliation import MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS
from scheduler.reserves import FleetConsolidationService
from scheduler.state import (
    RedisOrphanedContainerConfirmationRepository,
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerNetworkIpRepository,
    RedisWorkerPoolStateRepository,
)
from scheduler.worker_rollout import WorkerWorkloadDrainService
from storage.retention_settings import RetentionSettings

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings
from scheduler_app.capacity_interruptions import DatabaseCapacityInterruptionSource
from scheduler_app.fleet_coordinator import FleetCoordinator
from scheduler_app.fleet_housekeeping import FleetHousekeeping
from scheduler_app.fleet_services import (
    FleetAppServices,
    SchedulerCapacitySettings,
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)


@dataclass(slots=True)
class FleetRuntime:
    controller: FleetCoordinator
    owned_services: FleetAppServices | None = None
    reset_token_invalidation_on_close: bool = False

    @classmethod
    def create(
        cls,
        *,
        public_gateway_http_url: str,
        observability: SchedulerObservabilitySettings,
        storage: SchedulerStorageSettings,
        capacity: SchedulerCapacitySettings,
        managed_compute_reconcile_interval_seconds: float = (
            MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS
        ),
    ) -> FleetRuntime:
        database = DatabaseClient.from_settings(
            DatabaseSettings(application_name=DatabaseApplicationName.FleetController)
        )
        redis_client = RedisClient.from_settings()
        configure_token_invalidation(AuthTokenInvalidation.from_redis(redis_client))
        app_services: FleetAppServices | None = None
        try:
            app_services = FleetAppServices.create(
                database,
                redis_client=redis_client,
                gateway_origin=public_gateway_http_url,
                observability=observability,
                storage=storage,
                capacity=capacity,
            )
            runtime = cls.from_services(
                app_services,
                retention_settings=storage.retention,
                managed_compute_reconcile_interval_seconds=managed_compute_reconcile_interval_seconds,
            )

        except BaseException:
            configure_token_invalidation(None)
            if app_services is not None:
                app_services.close()
            else:
                try:
                    redis_client.close()
                finally:
                    database.dispose()
            raise
        runtime.owned_services = app_services
        runtime.reset_token_invalidation_on_close = True
        return runtime

    @classmethod
    def from_services(
        cls,
        services: FleetAppServices,
        *,
        retention_settings: RetentionSettings,
        managed_compute_reconcile_interval_seconds: float = (
            MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS
        ),
    ) -> FleetRuntime:
        redis_client = services.redis_client
        container_requests = _container_requests(services)
        compute_states = RedisComputeStateRepository(redis_client)
        pool_states = RedisWorkerPoolStateRepository(redis_client)
        worker_states = RedisSchedulerWorkerRepository(redis_client)
        container_states = RedisSchedulerContainerRepository(redis_client)
        preemption_recovery = PreemptedContainerService(
            services=services,
            stubs=services.control_plane_service,
        )
        capacity_controllers = SchedulerCapacityControllerProvider(
            services=services,
            compute_states=compute_states,
            workers=worker_states,
            containers=container_states,
        )
        agent_pool_service = SchedulerAgentPoolService(compute_states, worker_states)
        capacity_reservations = CapacityReservationService(
            RedisCapacityReservationRepository(redis_client),
            capacity_controllers.capacity_acquisition_controllers,
            DatabaseCapacityAllocationOwners(services.context.database),
        )
        dispatch_requests = replace(
            container_requests,
            capacity_reservations=capacity_reservations,
        )
        controller = FleetCoordinator(
            services=services,
            requests=dispatch_requests,
            workers=worker_states,
            wake=RedisWakeSignal(redis_client, CAPACITY_WAKE_SCOPE),
            preemptions=preemption_recovery,
            orphans=OrphanedContainerRecovery(
                services.containers,
                dispatch_requests,
                RedisOrphanedContainerConfirmationRepository(redis_client),
                RedisWorkerNetworkIpRepository(redis_client),
            ),
            compute_states=compute_states,
            pool_states=SchedulerPoolStateService(
                worker_states, container_states, pool_states, compute_states
            ),
            agent_pools=agent_pool_service,
            agent_configs=capacity_controllers.agent_pool_configs,
            reservations=capacity_reservations,
            drains=WorkerPoolDrainService(
                capacity_controllers.worker_pool_drain_controllers, capacity_reservations
            ),
            consolidation=FleetConsolidationService(
                compute=services.compute.maintenance,
                machine_units=services.compute.machines.provider_machine_unit,
                containers=DatabaseMachineContainers(services.context.database),
                workers=worker_states,
                stopper=services.containers,
                leases=capacity_reservations,
                state=services.compute.providers.reserve_state,
                cooldown_seconds=services.compute.providers.fleet_policy.consolidation_cooldown_seconds,
                deadline_seconds=services.compute.providers.fleet_policy.consolidation_deadline_seconds,
            )
            if services.compute.providers.reserve_state is not None
            else None,
            interruptions=SchedulerCapacityInterruptionService(
                SchedulerWorkerPreemptionService(
                    worker_states, container_states, services.containers
                ),
                worker_states,
                DatabaseCapacityInterruptionSource(services.context.database),
                SchedulerWorkerMaintenanceService(worker_states),
                workload_drains=WorkerWorkloadDrainService(
                    services.context.database, container_states
                ),
            ),
            housekeeping=FleetHousekeeping(services, retention_settings),
            managed_compute_reconcile_interval_seconds=managed_compute_reconcile_interval_seconds,
        )
        return cls(controller=controller)

    def close(self) -> None:
        owned_services = self.owned_services
        self.owned_services = None
        try:
            if owned_services is not None:
                owned_services.close()
        finally:
            if self.reset_token_invalidation_on_close:
                configure_token_invalidation(None)
                self.reset_token_invalidation_on_close = False

    def __enter__(self) -> FleetRuntime:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def _container_requests(services: FleetAppServices) -> SchedulerContainerRequestService:
    container_requests = services.containers.scheduler
    if not isinstance(container_requests, SchedulerContainerRequestService):
        msg = "scheduler app container service requires its injected scheduler repository"
        raise RuntimeError(msg)
    return container_requests
