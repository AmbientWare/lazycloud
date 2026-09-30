from __future__ import annotations

from dataclasses import dataclass, replace
from types import TracebackType

from control.placement import PlacementResolver
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal, WakeSignalWaiter
from execution.demand import PLACEMENT_WAKE_SCOPE
from execution.endpoints.service import EndpointControlService
from execution.functions.service import FunctionControlService
from execution.pods.service import PodControlService
from execution.services import ExecutionServices
from images.changes import ImageBuildChanges
from images.settings import ImageBuildContainerSettings
from images.submission import ImageBuildSubmissionService
from scheduler.adapters import DatabaseCapacityAllocationOwners, EndpointDispatchAutoscalingReader
from scheduler.autoscaling import (
    AutoscalingDriver,
    EndpointAutoscaler,
    FunctionAutoscaler,
    PodAutoscaler,
)
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.containers import CONTAINER_DISPATCH_WAKE_SCOPE, SchedulerContainerRequestService
from scheduler.cron import CronScheduler
from scheduler.service import Scheduler
from scheduler.services import SchedulerServices
from scheduler.state import RedisSchedulerContainerRepository, RedisSchedulerWorkerRepository
from worker_repository.image_build_dispatch import DurableImageBuildDispatch

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings
from scheduler_app.composition_settings import (
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)
from scheduler_app.services import SchedulerAppServices


@dataclass(slots=True)
class SchedulerRuntime:
    scheduler: Scheduler
    dispatch_wake: WakeSignalWaiter
    placement_wake: WakeSignalWaiter
    owned_services: SchedulerAppServices | None = None

    @classmethod
    def create(
        cls,
        *,
        public_gateway_http_url: str,
        observability: SchedulerObservabilitySettings,
        storage: SchedulerStorageSettings,
        image_build_container_settings: ImageBuildContainerSettings,
    ) -> SchedulerRuntime:
        database = DatabaseClient.from_settings(
            DatabaseSettings(application_name=DatabaseApplicationName.Scheduler)
        )
        redis_client = RedisClient.from_settings()
        app_services = None
        try:
            app_services = SchedulerAppServices.create(
                database,
                redis_client=redis_client,
                gateway_origin=public_gateway_http_url,
                observability=observability,
                storage=storage,
            )
            requests = app_services.containers.scheduler
            if not isinstance(requests, SchedulerContainerRequestService):
                raise RuntimeError("execution scheduler requires the durable request service")
            runtime = cls.from_services(
                scheduler_services=app_services,
                execution_services=app_services,
                redis_client=redis_client,
                container_requests=requests,
                image_build_container_settings=image_build_container_settings,
                placement_resolver=app_services.compute_policies,
            )
        except BaseException:
            if app_services is not None:
                app_services.close()
            else:
                try:
                    redis_client.close()
                finally:
                    database.dispose()
            raise
        runtime.owned_services = app_services
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
        placement_resolver: PlacementResolver,
    ) -> SchedulerRuntime:
        workers = RedisSchedulerWorkerRepository(redis_client)
        containers = RedisSchedulerContainerRepository(redis_client)
        requests = replace(
            container_requests,
            capacity_reservations=CapacityReservationService(
                RedisCapacityReservationRepository(redis_client),
                allocation_owners=DatabaseCapacityAllocationOwners(
                    scheduler_services.context.database
                ),
            ),
        )
        functions = FunctionControlService(execution_services)
        return cls(
            scheduler=Scheduler(
                services=scheduler_services,
                redis=redis_client,
                containers=requests,
                functions=functions,
                cron=CronScheduler(scheduler_services, redis_client, functions),
                image_builds=ImageBuildSubmissionService(
                    scheduler_services.context.database,
                    DurableImageBuildDispatch(
                        scheduler_services.context.database,
                        requests,
                        execution_services.containers,
                        image_build_container_settings,
                        placement_resolver,
                    ),
                    execution_services.object_storage.object_client,
                    ImageBuildChanges(redis_client),
                ),
                autoscalers=tuple(
                    AutoscalingDriver(
                        scheduler_services,
                        redis=redis_client,
                        workload=workload,
                        container_states=containers,
                        container_requests=workers,
                    )
                    for workload in (
                        FunctionAutoscaler(scheduler_services, functions=functions),
                        EndpointAutoscaler(
                            scheduler_services,
                            endpoints=EndpointControlService(execution_services),
                            dispatches=EndpointDispatchAutoscalingReader(
                                scheduler_services.context.database
                            ),
                        ),
                        PodAutoscaler(
                            scheduler_services,
                            redis=redis_client,
                            pods=PodControlService(execution_services, redis=redis_client),
                        ),
                    )
                ),
            ),
            dispatch_wake=RedisWakeSignal(redis_client, CONTAINER_DISPATCH_WAKE_SCOPE),
            placement_wake=RedisWakeSignal(redis_client, PLACEMENT_WAKE_SCOPE),
        )

    def close(self) -> None:
        services, self.owned_services = self.owned_services, None
        if services is not None:
            services.close()

    def __enter__(self) -> SchedulerRuntime:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
