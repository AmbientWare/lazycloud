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
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from observability.stream_state import RedisEventStreamRepository
from observability.usage import UsageService
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
from provider_stripe import StripeSettings
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

from billing import (
    BillingEnforcementService,
    BillingMeterOutboxService,
    BillingPlanChangeService,
    BillingReconciliationService,
    DatabaseBillingAdmission,
)
from database import DatabaseClient
from scheduler_app.execution_adapters import SchedulerWorkloadDirectoryAdapter


@dataclass(frozen=True, slots=True)
class SchedulerObservabilitySettings:
    workspace_changes: WorkspaceChangeStreamSettings


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
    meter_outbox: BillingMeterOutboxService
    plan_changes: BillingPlanChangeService
    billing_reconciliation: BillingReconciliationService
    billing_enforcement: BillingEnforcementService
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
        usage = UsageService(
            context,
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
        stripe_settings = StripeSettings()
        meter_outbox = _meter_outbox(context, events, stripe_settings)
        plan_changes = _plan_changes(context, events, stripe_settings)
        billing_reconciliation = _billing_reconciliation(context, events, stripe_settings)
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
            DatabaseBillingAdmission(),
            scheduler=container_scheduler,
            scheduler_cancellation=container_scheduler,
            event_bus=RedisEventBus(redis),
            workspace_changes=workspace_changes,
            runtime_state=container_runtime_state,
        )
        # After the container service, because stopping containers is the whole
        # of what this sweep does.
        billing_enforcement = _billing_enforcement(context, events, containers)
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
        cron_jobs = CronJobService(
            context,
            workspace_changes=workspace_changes,
        )
        deployments = DeploymentService(
            context,
            events,
            compute_policies,
            DeploymentRegistrationService(apps, control_plane),
            cron_jobs,
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
            metrics=MetricsService(),
            autoscaler_states=AutoscalerStateService(context),
            apps=apps,
            deployments=deployments,
            cron_jobs=cron_jobs,
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
            meter_outbox=meter_outbox,
            plan_changes=plan_changes,
            billing_reconciliation=billing_reconciliation,
            billing_enforcement=billing_enforcement,
            retention=retention,
            redis_client=redis,
        )

    def close(self) -> None:
        try:
            self.redis_client.close()
        finally:
            self.context.database.dispose()


def _meter_outbox(
    context: ServiceContext,
    events: EventService,
    settings: StripeSettings,
) -> BillingMeterOutboxService:
    """The sweep that delivers the priced usage the pricer already queued.

    Composed whether or not a payment credential exists. A deployment without one
    is misconfigured rather than in a mode: dropping the sweep for it would leave
    usage metered, priced, owed and never charged, with nothing said about why —
    and a sweep that is never going to run is exactly what nobody notices. The
    adapter is built on the first drain instead, which names the missing variable
    on every tick until it is set.
    """

    return BillingMeterOutboxService(
        database=context.database,
        payments=settings.provider_factory(),
        events=events,
    )


def _plan_changes(
    context: ServiceContext,
    events: EventService,
    settings: StripeSettings,
) -> BillingPlanChangeService:
    """The sweep that finishes plan changes whose outcome nobody recorded.

    Composed unconditionally for the same reason the outbox is, and the cost of
    dropping it is larger: an intent with no outcome is a customer who may
    already have paid for a plan this platform is not billing them on.
    """

    return BillingPlanChangeService(
        database=context.database,
        payments=settings.provider_factory(),
        events=events,
    )


def _billing_enforcement(
    context: ServiceContext,
    events: EventService,
    containers: ContainerService,
) -> BillingEnforcementService:
    """The pass that stops compute nobody can be billed for.

    Composed unconditionally and with no credential of its own: everything it
    decides is read from local rows, which is what lets it run every few seconds
    without the payment provider's availability deciding whether unfunded compute
    keeps running.
    """

    return BillingEnforcementService(
        database=context.database,
        containers=containers,
        events=events,
    )


def _billing_reconciliation(
    context: ServiceContext,
    events: EventService,
    settings: StripeSettings,
) -> BillingReconciliationService:
    """The pass that reports where the provider and this platform disagree.

    Composed unconditionally, again for the reason the outbox is: the whole
    point of this pass is that somebody is watching, and a deployment that
    silently had no watcher would be the failure it exists to catch.
    """

    return BillingReconciliationService(
        database=context.database,
        payments=settings.provider_factory(),
        events=events,
    )


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
