from __future__ import annotations

from dataclasses import dataclass

from compute.capacity_recovery import CAPACITY_WAKE_SCOPE
from compute.policy import WorkspaceComputePolicyService
from compute.reserve_state import RedisFleetReserveState
from control.apps import (
    DatabaseAppExecutionAdmission,
)
from control.deployments import CronJobService
from control.placement import PlacementResolver
from control.readers import DatabaseAppReader, DatabaseDeploymentReader
from control.service import ControlPlaneService
from coordination.event_bus import RedisEventBus
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from database.context import ServiceContext
from execution.containers.runtime_state import RedisContainerRuntimeStateRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.containers.service import ContainerService
from execution.demand import PLACEMENT_WAKE_SCOPE, ExecutionDemandService
from execution.task_progress import TaskProgressService
from execution.tasks import TaskService
from observability.events import EventService
from observability.metrics import MetricsService
from observability.stream_state import RedisEventStreamRepository
from observability.usage import UsageService
from observability.workspace_changes import WorkspaceChangeRepository, WorkspaceChangeService
from operations.container_shutdown import (
    ContainerShutdownService,
    DatabaseContainerStorageRelease,
    DatabaseDurableWorkerAbsence,
)
from scheduler.containers import (
    CONTAINER_DISPATCH_WAKE_SCOPE,
    SchedulerContainerRequestService,
)
from scheduler.disk_volume_attachments import DatabaseDiskVolumeAttachments
from scheduler.preemption import SchedulerGpuBackfillPreemptionService
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
)
from scheduler.workspace_owners import DatabaseWorkspaceOwners
from storage.service import ObjectStorage
from storage_client.s3 import S3ObjectStoreClient

from billing import (
    DatabaseBillingAdmission,
)
from database import DatabaseClient
from scheduler_app.composition_settings import (
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)


@dataclass(slots=True)
class SchedulerAppServices:
    context: ServiceContext
    payment_admission: DatabaseBillingAdmission
    events: EventService
    workspace_changes: WorkspaceChangeService
    metrics: MetricsService
    apps: DatabaseAppReader
    deployments: DatabaseDeploymentReader
    cron_jobs: CronJobService
    containers: ContainerService
    container_shutdowns: ContainerShutdownService
    control_plane_service: ControlPlaneService
    compute_policies: WorkspaceComputePolicyService
    tasks: TaskService
    execution_demand: ExecutionDemandService
    usage: UsageService
    object_storage: ObjectStorage
    object_store_client: S3ObjectStoreClient
    redis_client: RedisClient

    @property
    def placement_resolver(self) -> PlacementResolver:
        return self.compute_policies

    @classmethod
    def create(
        cls,
        database: DatabaseClient,
        *,
        redis_client: RedisClient,
        gateway_origin: str,
        observability: SchedulerObservabilitySettings,
        storage: SchedulerStorageSettings,
    ) -> SchedulerAppServices:
        context = ServiceContext.create(database)

        object_client = S3ObjectStoreClient.from_settings(storage.object_store)

        redis = redis_client

        stream_events = RedisEventStreamRepository(redis)

        events = EventService(context, stream_events=stream_events)

        workspace_changes = WorkspaceChangeService(
            WorkspaceChangeRepository(
                redis,
                max_length=observability.workspace_changes.max_length,
            )
        )

        compute_policies = WorkspaceComputePolicyService(context)

        control_plane = ControlPlaneService(
            context,
            workspace_storage_client=object_client,
            public_http_origin=gateway_origin,
            workspace_changes=workspace_changes,
        )

        container_repository = RedisSchedulerContainerRepository(redis)

        worker_repository = RedisSchedulerWorkerRepository(redis)

        tasks = TaskService(
            context,
            events,
            log_streams=stream_events,
            progress=TaskProgressService(context, container_repository, worker_repository),
            workspace_changes=workspace_changes,
        )

        usage = UsageService(
            context,
            workspace_changes=workspace_changes,
        )

        object_storage = ObjectStorage(
            context, object_client=object_client, default_bucket=storage.object_store.bucket
        )

        reserve_state = RedisFleetReserveState(redis)

        container_runtime_state = RedisContainerRuntimeStateRepository(redis)

        scheduling_persistence = ContainerSchedulingPersistenceService(
            context,
            events,
            workspace_changes,
            runtime_state=container_runtime_state,
        )

        container_scheduler = SchedulerContainerRequestService(
            worker_repository,
            container_repository,
            placement=None,
            failure_handler=scheduling_persistence,
            assignments=scheduling_persistence,
            usage=usage,
            dispatch_wake=RedisWakeSignal(redis, CONTAINER_DISPATCH_WAKE_SCOPE),
            capacity_wake=RedisWakeSignal(redis, CAPACITY_WAKE_SCOPE),
            lifecycle_events=stream_events,
            workspace_owners=DatabaseWorkspaceOwners(context),
            disk_volume_attachments=DatabaseDiskVolumeAttachments(context),
            reserve_state=reserve_state,
        )

        container_shutdowns = ContainerShutdownService(
            container_repository,
            RedisEventBus(redis),
            redis,
            storage_release=DatabaseContainerStorageRelease(context),
            durable_worker_absence=DatabaseDurableWorkerAbsence(context, worker_repository),
        )

        payment_admission = DatabaseBillingAdmission()
        containers = ContainerService(
            context,
            events,
            tasks,
            DatabaseAppExecutionAdmission(),
            payment_admission,
            scheduler=container_scheduler,
            scheduler_cancellation=container_scheduler,
            event_bus=RedisEventBus(redis),
            workspace_changes=workspace_changes,
            runtime_state=container_runtime_state,
            container_shutdowns=container_shutdowns,
            workers=worker_repository,
            placement_resolver=compute_policies,
        )

        container_scheduler.backfill_preemption = SchedulerGpuBackfillPreemptionService(
            worker_repository, container_repository, containers
        )

        cron_jobs = CronJobService(
            context,
            workspace_changes=workspace_changes,
        )

        return cls(
            context=context,
            payment_admission=payment_admission,
            events=events,
            workspace_changes=workspace_changes,
            metrics=MetricsService(),
            apps=DatabaseAppReader(context),
            deployments=DatabaseDeploymentReader(context),
            cron_jobs=cron_jobs,
            containers=containers,
            container_shutdowns=container_shutdowns,
            control_plane_service=control_plane,
            compute_policies=compute_policies,
            tasks=tasks,
            execution_demand=ExecutionDemandService(RedisWakeSignal(redis, PLACEMENT_WAKE_SCOPE)),
            usage=usage,
            object_storage=object_storage,
            object_store_client=object_client,
            redis_client=redis,
        )

    def close(self) -> None:
        try:
            self.object_store_client.close()
        finally:
            try:
                self.redis_client.close()
            finally:
                self.context.database.dispose()
