from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from shared.scheduling import (
    SchedulerWorkerRecord,
)
from worker.adapters import (
    ContainerIpResolver,
    SchedulerContainerRoutePublisher,
    SchedulerContainerRouteRepository,
    SchedulerSandboxPortPublisher,
    SchedulerWorkerAddressPublisher,
    WorkerContainerEventPublisher,
    WorkerFinalizationCleanup,
    WorkerGpuReleaser,
    WorkerNetworkPortExposer,
    WorkerNetworkTeardown,
    WorkerOomWatcherStopper,
    WorkerRouteIdentity,
    WorkerRuntimeContainerStopper,
)
from worker.container_client.control import ContainerServiceClient
from worker.container_execution import (
    ContainerAutomaticCheckpointCoordinator,
    ContainerCheckpointRestorer,
    ContainerCredentialHydrator,
    ContainerGpuAssigner,
    ContainerImageLoader,
    ContainerInstanceRecorder,
    ContainerLifecyclePublisher,
    ContainerLogCaptureService,
    ContainerMountPreparer,
    ContainerNetworkPreparer,
    ContainerPortAllocator,
    ContainerRootfsPreparer,
    ContainerRuntimeExecutor,
    ContainerSpecBuilder,
    ContainerWorkspaceStorageMounter,
    WorkerContainerExecutionService,
)
from worker.container_service.protocols import (
    WorkerContainerArchiveCreator,
    WorkerContainerCheckpointCreator,
    WorkerContainerInstanceStore,
    WorkerContainerRuntimeController,
    WorkerSandboxDockerLifecycle,
    WorkerSandboxLogSink,
    WorkerSandboxNetworkPolicyUpdater,
    WorkerSandboxPortPublisher,
    WorkerSandboxProcessManagerFactory,
)
from worker.container_service.service import WorkerContainerService
from worker.container_service.transport import WorkerContainerServiceTransport
from worker.event_bridge import WorkerSourceCacheReconciler, WorkerStreamEventHandler
from worker.events import WorkerBuildCancelRegistry, WorkerPoolMode, WorkerStreamEvent
from worker.finalization import (
    WorkerContainerFinalizationService,
)
from worker.image_build_execution import (
    WorkerImageArchivePublisher,
    WorkerImageBuilder,
    WorkerImageBuildExecutionService,
)
from worker.image_build_runtime_credentials import ImageBuildCredentialLoader
from worker.monitoring import ContainerRuntimeMonitor
from worker.repository_payloads import StreamWorkerEventsRequest
from worker.request_mounts import WorkerRequestMountCleaner
from worker.retention import WorkerRetentionService
from worker.scheduler_requests import (
    WorkerSchedulerRequestContainerRepository,
    WorkerSchedulerRequestProcessor,
    WorkerSchedulerRequestWorkerRepository,
)
from worker.source_code import SourceWorkspaceLifecycle
from worker.supervision import (
    WorkerContainerCostResolver,
    WorkerEventSink,
    WorkerSupervisionService,
    WorkerUsageRecorder,
)
from worker.worker_lifecycle import (
    WorkerCleanupAction,
    WorkerLifecycleOrchestrator,
    WorkerLifecycleRepository,
    WorkerSupervisionUsageEmitter,
)


class WorkerProcessWorkerRepository(
    WorkerSchedulerRequestWorkerRepository,
    WorkerLifecycleRepository,
    Protocol,
):
    pass


class WorkerProcessContainerRepository(
    WorkerSchedulerRequestContainerRepository,
    SchedulerContainerRouteRepository,
    Protocol,
):
    pass


class WorkerProcessEventSource(Protocol):
    def stream_worker_events(
        self,
        request: StreamWorkerEventsRequest,
    ) -> Iterable[WorkerStreamEvent]: ...

    def acknowledge_worker_event(self, event_id: str, worker_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerProcessExecutionDependencies:
    image_loader: ContainerImageLoader
    port_allocator: ContainerPortAllocator
    mount_preparer: ContainerMountPreparer
    rootfs_preparer: ContainerRootfsPreparer
    spec_builder: ContainerSpecBuilder
    runtime_executor: ContainerRuntimeExecutor
    runtime_controller: WorkerContainerRuntimeController
    network_preparer: ContainerNetworkPreparer | None = None
    workspace_storage_mounter: ContainerWorkspaceStorageMounter | None = None
    gpu_assigner: ContainerGpuAssigner | None = None
    instance_recorder: ContainerInstanceRecorder | None = None
    credential_hydrator: ContainerCredentialHydrator | None = None
    lifecycle_events: ContainerLifecyclePublisher | None = None
    runtime_monitor: ContainerRuntimeMonitor | None = None
    checkpoint_restorer: ContainerCheckpointRestorer | None = None
    automatic_checkpoints: ContainerAutomaticCheckpointCoordinator | None = None
    container_logs: ContainerLogCaptureService | None = None


@dataclass(frozen=True, slots=True)
class WorkerProcessContainerServiceDependencies:
    process_managers: WorkerSandboxProcessManagerFactory | None = None
    sandbox_docker: WorkerSandboxDockerLifecycle | None = None
    logs: WorkerSandboxLogSink | None = None
    checkpoints: WorkerContainerCheckpointCreator | None = None
    archives: WorkerContainerArchiveCreator | None = None
    network_policy: WorkerSandboxNetworkPolicyUpdater | None = None
    ports: WorkerSandboxPortPublisher | None = None
    port_exposer: WorkerNetworkPortExposer | None = None


@dataclass(frozen=True, slots=True)
class WorkerProcessFinalizationDependencies:
    container_ips: ContainerIpResolver | None = None
    gpu: WorkerGpuReleaser | None = None
    network: WorkerNetworkTeardown | None = None
    oom_watchers: WorkerOomWatcherStopper | None = None
    request_mounts: WorkerRequestMountCleaner | None = None
    source_workspaces: SourceWorkspaceLifecycle | None = None


@dataclass(frozen=True, slots=True)
class WorkerProcessImageBuildDependencies:
    build_cancels: WorkerBuildCancelRegistry | None = None
    image_builder: WorkerImageBuilder | None = None
    image_archive_publisher: WorkerImageArchivePublisher | None = None
    image_build_credential_loader: ImageBuildCredentialLoader | None = None


@dataclass(slots=True)
class WorkerProcessServices:
    identity: WorkerRouteIdentity
    workers: WorkerProcessWorkerRepository
    containers: WorkerProcessContainerRepository
    instances: WorkerContainerInstanceStore
    container_service: WorkerContainerService
    container_transport: WorkerContainerServiceTransport
    container_client: ContainerServiceClient
    execution: WorkerContainerExecutionService
    lifecycle: WorkerLifecycleOrchestrator
    worker_events: WorkerStreamEventHandler
    event_source: WorkerProcessEventSource | None
    processor: WorkerSchedulerRequestProcessor
    retention: WorkerRetentionService | None = None


def assemble_worker_process_services(
    *,
    identity: WorkerRouteIdentity,
    dependencies: WorkerProcessExecutionDependencies,
    workers: WorkerProcessWorkerRepository,
    containers: WorkerProcessContainerRepository,
    instances: WorkerContainerInstanceStore,
    event_sink: WorkerEventSink,
    usage_recorder: WorkerUsageRecorder,
    pool_mode: WorkerPoolMode,
    registration: SchedulerWorkerRecord | None,
    readiness_validator: Callable[[], None] | None = None,
    cost_resolver: WorkerContainerCostResolver | None = None,
    event_source: WorkerProcessEventSource | None = None,
    container_service_dependencies: WorkerProcessContainerServiceDependencies | None = None,
    finalization_dependencies: WorkerProcessFinalizationDependencies | None = None,
    image_build_dependencies: WorkerProcessImageBuildDependencies | None = None,
    source_cache_reconciler: WorkerSourceCacheReconciler | None = None,
    retention: WorkerRetentionService | None = None,
) -> WorkerProcessServices:
    worker_repository = workers
    container_repository = containers
    instance_store = instances
    container_service_dependencies = (
        container_service_dependencies or WorkerProcessContainerServiceDependencies()
    )
    finalization_dependencies = finalization_dependencies or WorkerProcessFinalizationDependencies()
    image_build_dependencies = image_build_dependencies or WorkerProcessImageBuildDependencies()
    if finalization_dependencies.source_workspaces is not None:
        finalization_dependencies.source_workspaces.prune_abandoned_temporary_paths(
            {instance.container_id for instance in instance_store.list_container_instances()}
        )
    runtime_stopper = WorkerRuntimeContainerStopper(
        dependencies.runtime_controller,
        instances=instance_store,
        sandbox_docker=container_service_dependencies.sandbox_docker,
        worker_id=identity.worker_id,
    )
    address_publisher = SchedulerWorkerAddressPublisher(identity, container_repository)
    build_cancels = image_build_dependencies.build_cancels or WorkerBuildCancelRegistry()
    usage_supervisor = WorkerSupervisionService(
        worker_id=identity.worker_id,
        event_sink=event_sink,
        usage_recorder=usage_recorder,
        container_stopper=runtime_stopper,
        cost_resolver=cost_resolver,
        pool_mode=pool_mode,
    )
    finalizer = WorkerContainerFinalizationService(
        container_repository,
        WorkerFinalizationCleanup(
            runtime=dependencies.runtime_controller,
            instances=instance_store,
            gpu=finalization_dependencies.gpu or dependencies.gpu_assigner,
            network=finalization_dependencies.network,
            oom_watchers=finalization_dependencies.oom_watchers,
            request_mounts=finalization_dependencies.request_mounts,
            checkpoint_signals=dependencies.automatic_checkpoints,
            sandbox_docker=container_service_dependencies.sandbox_docker,
            source_workspaces=finalization_dependencies.source_workspaces,
            workspace_storage=dependencies.workspace_storage_mounter,
            container_rootfs=dependencies.rootfs_preparer,
        ),
    )
    execution = WorkerContainerExecutionService(
        address_publisher=address_publisher,
        image_loader=dependencies.image_loader,
        port_allocator=dependencies.port_allocator,
        mount_preparer=dependencies.mount_preparer,
        rootfs_preparer=dependencies.rootfs_preparer,
        spec_builder=dependencies.spec_builder,
        runtime=dependencies.runtime_executor,
        finalizer=finalizer,
        route_publisher=SchedulerContainerRoutePublisher(
            identity,
            container_repository,
            container_ips=finalization_dependencies.container_ips,
        ),
        network_preparer=dependencies.network_preparer,
        workspace_storage_mounter=dependencies.workspace_storage_mounter,
        gpu_assigner=dependencies.gpu_assigner,
        instance_recorder=dependencies.instance_recorder,
        sandbox_docker_preparer=container_service_dependencies.sandbox_docker,
        credential_hydrator=dependencies.credential_hydrator,
        oom_supervisor=WorkerSupervisionService(
            worker_id=identity.worker_id,
            event_sink=event_sink,
            usage_recorder=usage_recorder,
            container_stopper=runtime_stopper,
            cost_resolver=cost_resolver,
            pool_mode=pool_mode,
        ),
        exit_events=WorkerContainerEventPublisher(event_sink, identity.worker_id),
        lifecycle_events=dependencies.lifecycle_events,
        runtime_monitor=dependencies.runtime_monitor,
        status_repository=container_repository,
        checkpoint_restorer=dependencies.checkpoint_restorer,
        automatic_checkpoints=dependencies.automatic_checkpoints,
        container_logs=dependencies.container_logs,
    )
    container_service = WorkerContainerService(
        instances=instance_store,
        process_managers=container_service_dependencies.process_managers,
        sandbox_docker=container_service_dependencies.sandbox_docker,
        runtime=dependencies.runtime_controller,
        logs=container_service_dependencies.logs,
        checkpoints=container_service_dependencies.checkpoints,
        archives=container_service_dependencies.archives,
        network_policy=container_service_dependencies.network_policy,
        ports=container_service_dependencies.ports
        or SchedulerSandboxPortPublisher(
            identity,
            container_repository,
            port_allocator=dependencies.port_allocator,
            network=container_service_dependencies.port_exposer,
        ),
    )
    transport = WorkerContainerServiceTransport(container_service)
    lifecycle = WorkerLifecycleOrchestrator(
        worker_id=identity.worker_id,
        repository=worker_repository,
        stopper=runtime_stopper,
        usage_emitter=(
            None
            if dependencies.runtime_monitor is not None
            else WorkerSupervisionUsageEmitter(usage_supervisor)
        ),
        registration=registration,
        readiness_validator=readiness_validator,
        cleanup_actions=(
            [
                WorkerCleanupAction(
                    name="request-mounts",
                    action=finalization_dependencies.request_mounts.unmount_all,
                )
            ]
            if finalization_dependencies.request_mounts is not None
            else []
        ),
    )
    image_builds = (
        WorkerImageBuildExecutionService(
            address_publisher=address_publisher,
            instances=instance_store,
            builder=image_build_dependencies.image_builder,
            publisher=image_build_dependencies.image_archive_publisher,
            credential_loader=image_build_dependencies.image_build_credential_loader,
        )
        if image_build_dependencies.image_builder is not None
        and image_build_dependencies.image_archive_publisher is not None
        else None
    )
    return WorkerProcessServices(
        identity=identity,
        workers=worker_repository,
        containers=container_repository,
        instances=instance_store,
        container_service=container_service,
        container_transport=transport,
        container_client=ContainerServiceClient(transport),
        execution=execution,
        lifecycle=lifecycle,
        worker_events=WorkerStreamEventHandler(
            container_stopper=runtime_stopper,
            build_cancels=build_cancels,
            source_cache=source_cache_reconciler,
            acknowledger=event_source,
            worker_id=identity.worker_id,
        ),
        event_source=event_source,
        processor=WorkerSchedulerRequestProcessor(
            worker_id=identity.worker_id,
            workers=worker_repository,
            containers=container_repository,
            execution=execution,
            lifecycle=lifecycle,
            image_builds=image_builds,
        ),
        retention=retention,
    )
