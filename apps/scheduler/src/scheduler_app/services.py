from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent.binary import AgentBinarySettings
from compute.aws_connections import AwsAccountConnectionDirectory
from compute.policy import WorkspaceComputePolicyService
from compute.reclaim import ComputeReclaimPolicy
from compute.request_placement import ComputeCapacityPlacementService
from compute.service import ComputeService
from compute.state import RedisComputeStateRepository
from control.apps import (
    AppService,
    DatabaseAppExecutionAdmission,
    DatabaseAppImageAvailability,
)
from control.custom_domains import CustomDomainService
from control.deployment_cleanup import AppDeploymentLifecycleService
from control.deployment_registration import DeploymentRegistrationService
from control.deployment_resources import DeploymentResourceService
from control.deployments import CronJobService, DeploymentService
from control.service import ControlPlaneService
from coordination.event_bus import RedisEventBus
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from database.context import ServiceContext
from database.tailnet_cleanup import DatabaseTailnetCleanupStore
from execution.collections.service import CollectionService
from execution.containers.runtime_state import RedisContainerRuntimeStateRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.containers.service import ContainerService
from execution.tasks import TaskService
from gateway.pool_bootstrap import pool_bootstrap_provisioner
from gateway.settings import GatewaySettings
from networking.settings import (
    BackendRouteSettings,
    TailnetControlSettings,
    TailnetRuntimeSettings,
)
from networking.tailnet_cleanup import TailnetCleanupCoordinator
from networking.tailnet_control import TailscaleTailnetControl
from observability.events import EventService
from observability.metrics import MetricsService
from observability.settings import (
    UsageMetricsSettings,
    UsagePricingSettings,
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from observability.stream_state import RedisEventStreamRepository
from observability.usage import UsageService
from observability.usage_exporter import UsageMetricsExporter
from observability.workspace_changes import WorkspaceChangeRepository, WorkspaceChangeService
from operations.app_lifecycle import ProductionAppExecutionLifecycleEffects
from operations.container_shutdown import (
    ContainerShutdownService,
    DatabaseDurableWorkerAbsence,
)
from provider_clients import (
    workspace_compute_provider_resolver,
)
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from provider_cloudflare import CloudflareSettings
from scheduler.autoscaler_states import AutoscalerStateService
from scheduler.capacity_reservations import RedisCapacityReservationRepository
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.compute_placement import SchedulerComputePlacement
from scheduler.containers import (
    CONTAINER_DISPATCH_WAKE_SCOPE,
    SchedulerContainerRequestService,
)
from scheduler.service import (
    SchedulerTailnetCleanupService,
    UnavailableTailnetCleanupService,
)
from scheduler.services import SchedulerWorkloadDirectory
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
)
from scheduler.workspace_owners import DatabaseWorkspaceOwners
from shared.checkpoints import checkpoint_recent_stub_key
from storage.image_archive import ImageArchiveSettings, ResolvedImageArchiveSettings
from storage.retention import (
    RetentionResult,
    RetentionService,
)
from storage.retention_settings import RetentionSettings
from storage.service import CacheStorage, ObjectStorage
from storage.volume_filesystem import (
    WorkspaceVolumeFilesystem,
    workspace_volume_store_resolver,
)
from storage.volume_metering import PersistentVolumeMeteringService
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings

from database import DatabaseClient
from scheduler_app.execution_adapters import SchedulerWorkloadDirectoryAdapter


@dataclass(frozen=True, slots=True)
class SchedulerObservabilitySettings:
    workspace_changes: WorkspaceChangeStreamSettings
    usage_metrics: UsageMetricsSettings
    usage_pricing: UsagePricingSettings


@dataclass(frozen=True, slots=True)
class SchedulerStorageSettings:
    object_store: S3ObjectStoreSettings
    image_archive: ImageArchiveSettings
    retention: RetentionSettings
    volume_metering: VolumeMeteringSettings


@dataclass(frozen=True, slots=True)
class SchedulerNetworkSettings:
    tailnet_runtime: TailnetRuntimeSettings
    tailnet_control: TailnetControlSettings
    backend_routes: BackendRouteSettings


@dataclass(frozen=True, slots=True)
class SchedulerCapacitySettings:
    aws_connections: AwsAccountConnectionSettings
    aws_capacity: AwsCapacitySettings
    agent_binaries: AgentBinarySettings
    reclaim: ComputeReclaimPolicy


@dataclass(slots=True)
class SchedulerAppServices:
    context: ServiceContext
    events: EventService
    workspace_changes: WorkspaceChangeService
    metrics: MetricsService
    autoscaler_states: AutoscalerStateService
    apps: AppService
    deployments: DeploymentService
    cron_jobs: CronJobService
    collections: CollectionService
    containers: ContainerService
    container_shutdowns: ContainerShutdownService
    scheduler_workloads: SchedulerWorkloadDirectory
    compute: ComputeService
    tailnet_cleanup: SchedulerTailnetCleanupService
    custom_domains: CustomDomainService
    tasks: TaskService
    usage: UsageService
    object_storage: ObjectStorage
    volume_metering: PersistentVolumeMeteringService
    retention: SchedulerRetention | None
    redis_client: RedisClient

    @classmethod
    def create(
        cls,
        database: DatabaseClient,
        *,
        root: Path | None = None,
        create_schema: bool = True,
        redis_client: RedisClient,
        gateway_origin: str,
        runtime_callback_origin: str,
        observability: SchedulerObservabilitySettings,
        storage: SchedulerStorageSettings,
        network: SchedulerNetworkSettings,
        capacity: SchedulerCapacitySettings,
    ) -> SchedulerAppServices:
        context = ServiceContext.create(database, root=root, create_schema=create_schema)
        image_archive_config = storage.image_archive.resolve(storage.object_store)
        redis = redis_client
        stream_events = RedisEventStreamRepository(redis)
        events = EventService(context, stream_events=stream_events)
        workspace_changes = WorkspaceChangeService(
            WorkspaceChangeRepository(
                redis,
                max_length=observability.workspace_changes.max_length,
            )
        )
        control_plane = ControlPlaneService(
            context,
            workspace_changes=workspace_changes,
        )
        scheduler_workloads = SchedulerWorkloadDirectoryAdapter(control_plane)
        tasks = TaskService(
            context,
            events,
            workspace_changes=workspace_changes,
        )
        compute_policies = WorkspaceComputePolicyService(context)
        usage_exporter = UsageMetricsExporter(observability.usage_metrics.to_sink_settings())
        usage = UsageService(
            context,
            exporter=usage_exporter,
            price_catalog=observability.usage_pricing.to_price_catalog(),
            workspace_changes=workspace_changes,
        )
        volume_metering = PersistentVolumeMeteringService.from_settings(
            context,
            filesystem=WorkspaceVolumeFilesystem(
                resolve_store=workspace_volume_store_resolver(
                    lambda workspace_id: control_plane.get_workspace(workspace_id).storage,
                    default_endpoint_url=storage.object_store.endpoint_url,
                    default_presigned_endpoint_url=storage.object_store.presigned_endpoint_url,
                )
            ),
            interval_seconds=storage.volume_metering.interval_seconds,
        )
        object_storage = ObjectStorage.from_settings(context, storage.object_store)
        retention = scheduler_retention(
            context=context,
            object_storage=object_storage,
            cache_storage=CacheStorage(context),
            settings=storage.retention,
            image_archive_settings=image_archive_config,
        )
        worker_repository = RedisSchedulerWorkerRepository(redis)
        container_repository = RedisSchedulerContainerRepository(redis)
        # See the API composition: the resolver exists only where connected AWS is
        # configured, and a half-configured deployment is rejected by settings.
        provider_resolver = (
            workspace_compute_provider_resolver(
                capacity.aws_capacity,
                capacity.agent_binaries,
                connections=AwsAccountConnectionDirectory(context).list_for_workspace,
                gateway_origin=gateway_origin,
                internal_origin=runtime_callback_origin,
                presigned_origin=storage.object_store.presigned_endpoint_url or "",
                tailnet_runtime=network.tailnet_runtime,
                tailnet_control=network.tailnet_control,
                backend_route=network.backend_routes,
            )
            if capacity.aws_connections.configured
            else None
        )
        agent_version, agent_sha256 = (
            capacity.agent_binaries.require_amd64() if provider_resolver is not None else ("", "")
        )

        pool_bootstrap = (
            pool_bootstrap_provisioner(
                context,
                # A node in a customer VPC holds no tailnet session when it
                # first reports, so this is the public origin. The runtime
                # callback origin stays worker-facing and is not interchangeable
                # here. The API must pass the same one: a disagreement shows up
                # as launch templates alternating between versions.
                control_plane_url=gateway_origin,
                agent_version=agent_version,
                agent_sha256=agent_sha256,
                agent_binary_url=capacity.aws_capacity.agent_binary_url,
                worker_image_digest=capacity.aws_capacity.worker_image_digest,
                tailnet_control=network.tailnet_control,
            )
            if provider_resolver is not None
            else None
        )

        scheduler_hooks = SchedulerComputeHooks(
            RedisComputeStateRepository(redis),
            worker_repository,
        )
        compute_policies.worker_state = scheduler_hooks
        compute = ComputeService(
            context,
            provider_resolver=provider_resolver,
            pool_bootstrap_factory=pool_bootstrap if provider_resolver is not None else None,
            usage_exporter=usage_exporter,
            scheduler_hooks=scheduler_hooks,
            workspace_changes=workspace_changes,
            reclaim=capacity.reclaim,
            capacity_owner_mutations=RedisCapacityReservationRepository(redis),
        )
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
            placement=SchedulerComputePlacement(
                ComputeCapacityPlacementService(
                    context,
                    compute_policies,
                    compute,
                )
            ),
            failure_handler=scheduling_persistence,
            assignments=scheduling_persistence,
            usage=usage,
            dispatch_wake=RedisWakeSignal(redis, CONTAINER_DISPATCH_WAKE_SCOPE),
            lifecycle_events=stream_events,
            workspace_owners=DatabaseWorkspaceOwners(context),
        )
        containers = ContainerService(
            context,
            events,
            tasks,
            DatabaseAppExecutionAdmission(),
            scheduler=container_scheduler,
            scheduler_cancellation=container_scheduler,
            event_bus=RedisEventBus(redis),
            workspace_changes=workspace_changes,
            runtime_state=container_runtime_state,
        )
        container_shutdowns = ContainerShutdownService(
            container_repository,
            RedisEventBus(redis),
            redis,
            DatabaseDurableWorkerAbsence(context, worker_repository),
        )
        deployment_lifecycle = AppDeploymentLifecycleService(
            context,
            workspace_changes=workspace_changes,
        )
        apps = AppService(
            context,
            deployment_lifecycle,
            ProductionAppExecutionLifecycleEffects(
                context,
                containers,
                tasks,
                redis,
                container_shutdowns,
            ),
            DatabaseAppImageAvailability(),
            workspace_changes=workspace_changes,
        )
        deployments = DeploymentService(
            context,
            events,
            compute_policies,
            DeploymentRegistrationService(apps, control_plane),
            workspace_changes=workspace_changes,
        )
        _, tailnet_cleanup = scheduler_tailnet_services(
            context=context,
            runtime_settings=network.tailnet_runtime,
            control_settings=network.tailnet_control,
        )
        return cls(
            context=context,
            events=events,
            workspace_changes=workspace_changes,
            metrics=MetricsService(context),
            autoscaler_states=AutoscalerStateService(context),
            apps=apps,
            deployments=deployments,
            cron_jobs=CronJobService(
                context,
                deployments,
                workspace_changes=workspace_changes,
            ),
            collections=CollectionService(context),
            containers=containers,
            container_shutdowns=container_shutdowns,
            scheduler_workloads=scheduler_workloads,
            compute=compute,
            tailnet_cleanup=tailnet_cleanup,
            custom_domains=CustomDomainService(
                context=context,
                provider_factory=CloudflareSettings().provider,
                platform_base_domain=GatewaySettings().public_base_domain,
            ),
            tasks=tasks,
            usage=usage,
            object_storage=object_storage,
            volume_metering=volume_metering,
            retention=retention,
            redis_client=redis,
        )

    def close(self) -> None:
        try:
            self.redis_client.close()
        finally:
            self.context.database.dispose()


@dataclass(slots=True)
class SchedulerRetention:
    service: RetentionService
    deployment_resources: DeploymentResourceService

    def protected_checkpoint_stub_keys(self, *, now: datetime | None = None) -> list[str]:
        del now
        return sorted(
            checkpoint_recent_stub_key(resource.stub.workspace_id, resource.stub.id)
            for resource in self.deployment_resources.list(workspace=None, active=True)
        )

    def reconcile(self, *, now: datetime | None = None) -> RetentionResult:
        return self.service.reconcile(
            active_recent_stub_keys=self.protected_checkpoint_stub_keys(now=now),
            now=now,
        )


def scheduler_retention(
    *,
    context: ServiceContext,
    object_storage: ObjectStorage,
    cache_storage: CacheStorage,
    settings: RetentionSettings,
    image_archive_settings: ResolvedImageArchiveSettings,
) -> SchedulerRetention | None:
    if not settings.enabled:
        return None
    return SchedulerRetention(
        service=RetentionService(
            context=context,
            object_storage=object_storage,
            cache_storage=cache_storage,
            config=settings.service_config(
                checkpoint_bucket=object_storage.default_bucket,
            ),
            image_archive_settings=image_archive_settings,
            image_archive_client=S3ObjectStoreClient.from_settings(image_archive_settings.storage),
        ),
        deployment_resources=DeploymentResourceService(context),
    )


def scheduler_tailnet_services(
    *,
    context: ServiceContext,
    runtime_settings: TailnetRuntimeSettings,
    control_settings: TailnetControlSettings,
) -> tuple[TailscaleTailnetControl | None, SchedulerTailnetCleanupService]:
    active_control = TailscaleTailnetControl(control_settings.to_control_config())
    cleanup_control = active_control
    cleanup_store = DatabaseTailnetCleanupStore(context)
    cleanup = (
        TailnetCleanupCoordinator(cleanup_store, cleanup_control)
        if cleanup_control is not None
        else UnavailableTailnetCleanupService(cleanup_store)
    )
    return active_control, cleanup
