from __future__ import annotations

from dataclasses import dataclass, replace
from types import TracebackType

from compute.capacity_recovery import CAPACITY_WAKE_SCOPE
from compute.state import RedisComputeStateRepository
from control.custom_domains import CustomDomainService
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from execution.containers.preemption import PreemptedContainerService, PreemptionServices
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
from scheduler.containers import (
    SchedulerContainerRequestService,
)
from scheduler.fleet_controller import FleetController
from scheduler.pool_drain import WorkerPoolDrainService
from scheduler.pool_state import SchedulerPoolStateService
from scheduler.preemption import (
    SchedulerCapacityInterruptionService,
    SchedulerWorkerMaintenanceService,
    SchedulerWorkerPreemptionService,
)
from scheduler.reconciliation import (
    MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS,
    SchedulerBillingEnforcementService,
    SchedulerBillingPaymentsService,
    SchedulerBillingReconciliationService,
    SchedulerCapacityControls,
    SchedulerDiskDeletionService,
    SchedulerDiskVolumeService,
    SchedulerEmailOutboxService,
    SchedulerMaintenanceControls,
    SchedulerMeterOutboxService,
    SchedulerPlanChangeService,
    SchedulerRetentionService,
    SchedulerStateStores,
    SchedulerStorageAccessService,
    SchedulerVolumeDeletionService,
    SchedulerVolumeMeteringService,
    SchedulerWorkloadControls,
)
from scheduler.reserves import FleetConsolidationService
from scheduler.services import FleetServices
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
from scheduler_app.fleet_services import (
    FleetAppServices,
    SchedulerCapacitySettings,
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)


@dataclass(slots=True)
class FleetRuntime:
    controller: FleetController
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
                scheduler_services=app_services,
                execution_services=app_services,
                redis_client=app_services.redis_client,
                container_requests=_container_requests(app_services),
                retention_settings=storage.retention,
                volume_metering=app_services.volume_metering,
                storage_access=app_services.storage_access,
                volume_deletion=app_services.volume_deletion,
                disk_deletion=app_services.disk_deletion,
                disk_volumes=app_services.disk_volumes,
                meter_outbox=app_services.meter_outbox,
                email_outbox=app_services.email_outbox,
                plan_changes=app_services.plan_changes,
                billing_reconciliation=app_services.billing_reconciliation,
                billing_payments=app_services.billing_payments,
                billing_enforcement=app_services.billing_enforcement,
                retention=app_services.retention,
                custom_domains=app_services.custom_domains,
                managed_compute_reconcile_interval_seconds=(
                    managed_compute_reconcile_interval_seconds
                ),
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
        *,
        scheduler_services: FleetServices,
        execution_services: PreemptionServices,
        redis_client: RedisClient,
        container_requests: SchedulerContainerRequestService,
        retention_settings: RetentionSettings,
        volume_metering: SchedulerVolumeMeteringService,
        storage_access: SchedulerStorageAccessService | None,
        volume_deletion: SchedulerVolumeDeletionService,
        disk_deletion: SchedulerDiskDeletionService,
        disk_volumes: SchedulerDiskVolumeService,
        meter_outbox: SchedulerMeterOutboxService,
        email_outbox: SchedulerEmailOutboxService,
        plan_changes: SchedulerPlanChangeService,
        billing_reconciliation: SchedulerBillingReconciliationService,
        billing_payments: SchedulerBillingPaymentsService,
        billing_enforcement: SchedulerBillingEnforcementService,
        retention: SchedulerRetentionService | None,
        custom_domains: CustomDomainService,
        managed_compute_reconcile_interval_seconds: float = (
            MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS
        ),
    ) -> FleetRuntime:
        compute_states = RedisComputeStateRepository(redis_client)
        pool_states = RedisWorkerPoolStateRepository(redis_client)
        worker_states = RedisSchedulerWorkerRepository(redis_client)
        container_states = RedisSchedulerContainerRepository(redis_client)
        preemption_recovery = PreemptedContainerService(
            services=execution_services,
            stubs=scheduler_services.control_plane_service,
        )
        capacity_controllers = SchedulerCapacityControllerProvider(
            services=scheduler_services,
            compute_states=compute_states,
            workers=worker_states,
            containers=container_states,
        )
        agent_pool_service = SchedulerAgentPoolService(compute_states, worker_states)
        capacity_reservations = CapacityReservationService(
            RedisCapacityReservationRepository(redis_client),
            capacity_controllers.capacity_acquisition_controllers,
            DatabaseCapacityAllocationOwners(scheduler_services.context.database),
        )
        dispatch_requests = replace(
            container_requests,
            capacity_reservations=capacity_reservations,
        )
        controller = FleetController(
            services=scheduler_services,
            managed_compute_reconcile_interval_seconds=(managed_compute_reconcile_interval_seconds),
            workloads=SchedulerWorkloadControls(
                containers=dispatch_requests,
                capacity_wake=RedisWakeSignal(redis_client, CAPACITY_WAKE_SCOPE),
                preemption_recovery=preemption_recovery,
            ),
            states=SchedulerStateStores(
                compute=compute_states,
                pools=SchedulerPoolStateService(
                    worker_states,
                    container_states,
                    pool_states,
                    compute_states,
                ),
                orphaned_container_networks=RedisWorkerNetworkIpRepository(redis_client),
                orphaned_container_confirmations=(
                    RedisOrphanedContainerConfirmationRepository(redis_client)
                ),
                cron_job_locks=redis_client,
            ),
            capacity=SchedulerCapacityControls(
                agent_pools=agent_pool_service,
                agent_pool_configs=capacity_controllers.agent_pool_configs,
                capacity_reservations=capacity_reservations,
                worker_pool_drain=WorkerPoolDrainService(
                    capacity_controllers.worker_pool_drain_controllers,
                    capacity_reservations,
                ),
                consolidation=(
                    FleetConsolidationService(
                        compute=scheduler_services.compute,
                        containers=DatabaseMachineContainers(scheduler_services.context.database),
                        workers=worker_states,
                        stopper=scheduler_services.containers,
                        leases=capacity_reservations,
                        state=scheduler_services.compute.reserve_state,
                        cooldown_seconds=(
                            scheduler_services.compute.fleet_policy.consolidation_cooldown_seconds
                        ),
                        deadline_seconds=(
                            scheduler_services.compute.fleet_policy.consolidation_deadline_seconds
                        ),
                    )
                    if scheduler_services.compute.reserve_state is not None
                    else None
                ),
                capacity_interruptions=SchedulerCapacityInterruptionService(
                    SchedulerWorkerPreemptionService(
                        worker_states,
                        container_states,
                        scheduler_services.containers,
                    ),
                    worker_states,
                    DatabaseCapacityInterruptionSource(
                        scheduler_services.context.database,
                    ),
                    SchedulerWorkerMaintenanceService(worker_states),
                    workload_drains=WorkerWorkloadDrainService(
                        scheduler_services.context.database, container_states
                    ),
                ),
            ),
            maintenance=SchedulerMaintenanceControls(
                storage_access=storage_access,
                volume_metering=volume_metering,
                volume_deletion=volume_deletion,
                disk_deletion=disk_deletion,
                disk_volumes=disk_volumes,
                meter_outbox=meter_outbox,
                email_outbox=email_outbox,
                plan_changes=plan_changes,
                billing_reconciliation=billing_reconciliation,
                billing_payments=billing_payments,
                billing_enforcement=billing_enforcement,
                retention=retention,
                custom_domains=custom_domains,
            ),
            retention_interval_seconds=retention_settings.interval_seconds,
            retention_retry_initial_seconds=(retention_settings.retry_initial_seconds),
            retention_retry_max_seconds=retention_settings.retry_max_seconds,
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
