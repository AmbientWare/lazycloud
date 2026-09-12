from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType

from compute.state import RedisComputeStateRepository
from control.custom_domains import CustomDomainService
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from execution.containers.preemption import PreemptedContainerService
from execution.endpoints.service import EndpointControlService
from execution.functions.service import FunctionControlService
from execution.pods.service import PodControlService
from execution.services import ExecutionServices
from identity.token_invalidation import AuthTokenInvalidation, configure_token_invalidation
from images.settings import ImageBuildContainerSettings
from images.submission import ImageBuildSubmissionService
from scheduler.adapters import (
    DatabaseCapacityAllocationOwners,
    EndpointDispatchAutoscalingReader,
)
from scheduler.agent_pool import SchedulerAgentPoolService
from scheduler.autoscaling import (
    AutoscalingDriver,
    EndpointAutoscaler,
    FunctionAutoscaler,
    PodAutoscaler,
)
from scheduler.autoscaling_targets import AutoscalingTargetService
from scheduler.capacity_controls import SchedulerCapacityControllerProvider
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.containers import (
    CONTAINER_DISPATCH_WAKE_SCOPE,
    SchedulerContainerRequestService,
)
from scheduler.pool_drain import WorkerPoolDrainService
from scheduler.pool_state import SchedulerPoolStateService
from scheduler.preemption import (
    SchedulerCapacityInterruptionService,
    SchedulerWorkerMaintenanceService,
    SchedulerWorkerPreemptionService,
)
from scheduler.service import (
    MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS,
    Scheduler,
    SchedulerBillingEnforcementService,
    SchedulerBillingPaymentsService,
    SchedulerBillingReconciliationService,
    SchedulerCapacityControls,
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
from scheduler.services import SchedulerServices
from scheduler.state import (
    RedisOrphanedContainerConfirmationRepository,
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerNetworkIpRepository,
    RedisWorkerPoolStateRepository,
)
from scheduler.worker_rollout import WorkerWorkloadDrainService
from storage.retention_settings import RetentionSettings
from worker_repository.image_build_dispatch import DurableImageBuildDispatch

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings
from scheduler_app.capacity_interruptions import DatabaseCapacityInterruptionSource
from scheduler_app.services import (
    SchedulerAppServices,
    SchedulerCapacitySettings,
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)


@dataclass(slots=True)
class SchedulerRuntime:
    scheduler: Scheduler
    owned_services: SchedulerAppServices | None = None
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
        image_build_container_settings: ImageBuildContainerSettings,
    ) -> SchedulerRuntime:
        database = DatabaseClient.from_settings(
            DatabaseSettings(application_name=DatabaseApplicationName.Scheduler)
        )
        redis_client = RedisClient.from_settings()
        configure_token_invalidation(AuthTokenInvalidation.from_redis(redis_client))
        app_services: SchedulerAppServices | None = None
        try:
            app_services = SchedulerAppServices.create(
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
                image_build_container_settings=image_build_container_settings,
                retention_settings=storage.retention,
                volume_metering=app_services.volume_metering,
                storage_access=app_services.storage_access,
                volume_deletion=app_services.volume_deletion,
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
        scheduler_services: SchedulerServices,
        execution_services: ExecutionServices,
        redis_client: RedisClient,
        container_requests: SchedulerContainerRequestService,
        image_build_container_settings: ImageBuildContainerSettings,
        retention_settings: RetentionSettings,
        volume_metering: SchedulerVolumeMeteringService,
        storage_access: SchedulerStorageAccessService | None,
        volume_deletion: SchedulerVolumeDeletionService,
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
    ) -> SchedulerRuntime:
        compute_states = RedisComputeStateRepository(redis_client)
        pool_states = RedisWorkerPoolStateRepository(redis_client)
        worker_states = RedisSchedulerWorkerRepository(redis_client)
        container_states = RedisSchedulerContainerRepository(redis_client)
        function_control = FunctionControlService(
            execution_services,
        )
        endpoint_control = EndpointControlService(
            execution_services,
        )
        endpoint_dispatches = EndpointDispatchAutoscalingReader(execution_services.context.database)
        pod_control = PodControlService(execution_services, redis=redis_client)
        preemption_recovery = PreemptedContainerService(
            services=execution_services,
            stubs=scheduler_services.scheduler_workloads,
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
        dispatch_requests = _container_requests_with_capacity(
            container_requests,
            capacity_reservations=capacity_reservations,
        )
        scheduler = Scheduler(
            services=scheduler_services,
            managed_compute_reconcile_interval_seconds=(managed_compute_reconcile_interval_seconds),
            workloads=SchedulerWorkloadControls(
                image_builds=ImageBuildSubmissionService(
                    scheduler_services.context.database,
                    DurableImageBuildDispatch(
                        scheduler_services.context.database,
                        dispatch_requests,
                        execution_services.containers,
                        image_build_container_settings,
                    ),
                ),
                containers=dispatch_requests,
                dispatch_wake=RedisWakeSignal(redis_client, CONTAINER_DISPATCH_WAKE_SCOPE),
                autoscaling_targets=AutoscalingTargetService(scheduler_services.context),
                function_autoscaler=AutoscalingDriver(
                    scheduler_services,
                    redis=redis_client,
                    workload=FunctionAutoscaler(scheduler_services, functions=function_control),
                    container_states=container_states,
                    container_requests=worker_states,
                ),
                endpoints=AutoscalingDriver(
                    scheduler_services,
                    redis=redis_client,
                    workload=EndpointAutoscaler(
                        scheduler_services,
                        endpoints=endpoint_control,
                        dispatches=endpoint_dispatches,
                    ),
                    container_states=container_states,
                    container_requests=worker_states,
                ),
                pods=AutoscalingDriver(
                    scheduler_services,
                    redis=redis_client,
                    workload=PodAutoscaler(
                        scheduler_services,
                        redis=redis_client,
                        pods=pod_control,
                    ),
                    container_states=container_states,
                    container_requests=worker_states,
                ),
                pod_control=pod_control,
                functions=function_control,
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
        return cls(scheduler=scheduler)

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

    def __enter__(self) -> SchedulerRuntime:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def _container_requests(services: SchedulerAppServices) -> SchedulerContainerRequestService:
    container_requests = services.containers.scheduler
    if not isinstance(container_requests, SchedulerContainerRequestService):
        msg = "scheduler app container service requires its injected scheduler repository"
        raise RuntimeError(msg)
    return container_requests


def _container_requests_with_capacity(
    base: SchedulerContainerRequestService,
    *,
    capacity_reservations: CapacityReservationService,
) -> SchedulerContainerRequestService:
    return SchedulerContainerRequestService(
        workers=base.workers,
        containers=base.containers,
        placement=base.placement,
        failure_handler=base.failure_handler,
        assignments=base.assignments,
        dispatch_wake=base.dispatch_wake,
        lifecycle_events=base.lifecycle_events,
        workspace_owners=base.workspace_owners,
        capacity_reservations=capacity_reservations,
        backfill_preemption=base.backfill_preemption,
        usage=base.usage,
        requeue_delay_seconds=base.requeue_delay_seconds,
        max_retry_count=base.max_retry_count,
        max_retry_age_seconds=base.max_retry_age_seconds,
        claim_lease_seconds=base.claim_lease_seconds,
    )
