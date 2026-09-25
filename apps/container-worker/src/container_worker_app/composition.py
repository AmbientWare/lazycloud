from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from cache.server import (
    FileCacheServer,
    WorkerCacheEndpoint,
    WorkerCacheHttpClient,
)
from foundation.process import ProcessTimeoutError, run_process
from networking.internal_http import InternalHttpClient
from shared.agent_connections import AGENT_TUNNEL_CONTROL_PORT, AGENT_TUNNEL_CONTROL_URL
from shared.app_identity import AGENT_CONTAINER_TMP_PATH, WORKER_ADMISSION_WAITING_FILE
from shared.disks import DiskStorage, disk_capacity_bytes
from shared.identity import TokenKind
from shared.placement import PlacementKind
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.step_timings import StepTimings
from worker.adapters import WorkerRouteIdentity
from worker.automatic_checkpoints import WorkerAutomaticCheckpointService
from worker.cache_assets import (
    WorkspaceGeeseFsStorageConfig,
    WorkspaceStorageConfig,
)
from worker.checkpoint_activity import CheckpointLeaseRegistry
from worker.checkpoint_restore import RuntimeCheckpointRestorer
from worker.checkpoint_transfer import RemoteCheckpointPersister, RemoteCheckpointRestoreSource
from worker.container_checkpoints import (
    RuntimeCheckpointCreator,
)
from worker.container_logs import WorkerContainerLogCaptureService
from worker.container_metrics import (
    CgroupContainerMetricsSourceFactory,
    WorkerContainerMetricsService,
)
from worker.container_rootfs import ContainerRootfsOverlayManager
from worker.container_service.protocols import WorkerContainerInstanceStore
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.container_service.supervisor_process_manager import (
    SupervisorSandboxProcessManagerFactory,
)
from worker.container_startup import (
    HostPortAllocator,
    WorkerImageArchiveMounter,
    WorkerImageArchiveSourceLoader,
    WorkerImageStartupLoader,
    WorkerRequestMountPreparer,
)
from worker.credential_hydration import WorkerCredentialHydrator
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.disk_volumes import DiskVolumeMounts
from worker.durable_disks import (
    DEFAULT_DISK_RUN_ROOT,
    DiskEngine,
    WorkerDurableDiskService,
    disk_layout,
)
from worker.execution import (
    GatewayEndpointSettings,
    GatewayServiceSettings,
)
from worker.filesystem_images import ContainerFilesystemExporter
from worker.gpu import (
    DynamicGpuAllocationManager,
    GpuAllocationManager,
    NvidiaGpuIndexProvider,
    WorkerGpuRuntimeAssigner,
)
from worker.image_build_execution import (
    BuildahWorkerImageBuilder,
    RepositoryImageBuildContextLoader,
    RepositoryWorkerImageArchivePublisher,
)
from worker.image_build_runtime_credentials import RemoteImageBuildCredentialLoader
from worker.image_build_scratch import ImageBuildScratchManager
from worker.image_lifecycle import ImageArchiveStorageMode
from worker.image_runtime import ImageRuntimeClient
from worker.managed_runtime import MANAGED_RUNTIME_IMAGE_ROOT
from worker.monitoring import (
    AsyncContainerLifecycleSink,
    ContainerRuntimeMonitorSettings,
    WorkerContainerRuntimeMonitor,
)
from worker.network_backend import (
    AgentBridgeNetworkBackend,
    AgentBridgeNetworkConfig,
    SchedulerNetworkIpAllocator,
)
from worker.network_egress import WorkerNetworkEgressCounters
from worker.oci_runtime import (
    OciRuntimeCommandController,
    OciRuntimeSpecBuilder,
)
from worker.repository_client import (
    RemoteAutomaticCheckpointCreationLeaseCoordinator,
    RemoteCheckpointStateSink,
    RemoteContainerLifecycleSink,
    RemoteContainerLogSink,
    RemoteContainerMetricsSink,
    RemoteContainerSshIdentitySource,
    RemoteSandboxProcessLogSink,
    RemoteSchedulerContainerRepository,
    RemoteSchedulerWorkerRepository,
    RemoteWorkerCredentialService,
    RemoteWorkerEventSink,
    RemoteWorkerNetworkIpRepository,
    RemoteWorkerRepositoryState,
    RemoteWorkerUsageRecorder,
    WorkerRepositoryHttpClient,
    build_worker_repository_http_client,
)
from worker.request_mounts import WorkerRequestMountLifecycle, WorkerRequestMountManager
from worker.retention import WorkerRetentionConfig, WorkerRetentionService
from worker.runtime_config import (
    OciRuntimeName,
    RuntimeAvailability,
    RuntimeAvailabilityStatus,
    RuntimeBinaryConfig,
    RuntimeUnavailableError,
    runtime_availability,
)
from worker.sandbox_docker import WorkerSandboxDockerService
from worker.source_cache_cleanup import WorkerSourceCacheIdentity
from worker.source_code import DEFAULT_SOURCE_CACHE_ROOT, SourceCodePackageMaterializer
from worker.supervision import (
    WorkerSupervisionService,
)
from worker.worker_lifecycle import WorkerCleanupAction
from worker.workspace_credential_refresh import WorkspaceCredentialRefresher
from worker.workspace_storage import WorkerWorkspaceStorageManager

from .container_instances import (
    ContainerInstanceRuntimeResolver,
    OciContainerServiceInstanceRecorder,
)
from .image_archives import (
    BrokeredClipImageMounter,
    BrokeredImageArchiveSourceLoader,
    CacheServerImageArchiveMetadataProvider,
)
from .image_cache import ImageContentCacheService
from .image_runtime import ImageRuntimeProcess
from .process_assembly import (
    WorkerProcessContainerServiceDependencies,
    WorkerProcessExecutionDependencies,
    WorkerProcessFinalizationDependencies,
    WorkerProcessImageBuildDependencies,
    WorkerProcessServices,
    assemble_worker_process_services,
)
from .settings import WorkerSettings

LOGGER = logging.getLogger(__name__)
RUNTIME_VERSION_PROBE_TIMEOUT_SECONDS = 5.0
GPU_PRESENCE_WAIT_SECONDS = 5.0
"""How long readiness waits for nvidia-smi to list every GPU before it fails."""


def build_worker_process_services(
    *,
    settings: WorkerSettings,
    image_mounter: WorkerImageArchiveMounter | None = None,
    image_runtime_client: ImageRuntimeClient | None = None,
    image_source_loader: WorkerImageArchiveSourceLoader | None = None,
    mountpoint_backend: WorkerRequestMountLifecycle | None = None,
    repository_client: WorkerRepositoryHttpClient | None = None,
    runtime_configs: dict[OciRuntimeName, RuntimeBinaryConfig] | None = None,
) -> WorkerProcessServices:
    config = settings
    configuration = config.configuration
    execution = configuration.execution
    paths = configuration.paths
    identity = _worker_identity(config)
    internal_http = _internal_http_client(config)
    # A held worker reports waiting only once its local readiness work succeeded,
    # so its reserve never hibernates with a worker that could not start.
    readiness: Future[None] = Future()
    repository = repository_client or build_worker_repository_http_client(
        endpoint=AGENT_TUNNEL_CONTROL_URL,
        token=config.worker_token,
        timeout_seconds=config.worker_repository_timeout_seconds,
        admission_hold_seconds=config.admission_hold_seconds,
        admission_waiting_file=(
            Path(AGENT_CONTAINER_TMP_PATH) / WORKER_ADMISSION_WAITING_FILE
            if config.admission_hold_seconds
            else None
        ),
        admission_readiness=readiness,
        http=internal_http,
    )
    image_build_scratch = ImageBuildScratchManager(
        root=paths.image_build_root.expanduser().resolve(),
        worker_id=identity.worker_id,
        max_bytes=configuration.image_build.scratch_max_bytes,
        per_build_max_bytes=configuration.image_build.per_build_max_bytes,
        minimum_free_bytes=configuration.image_build.minimum_free_bytes,
        stale_seconds=configuration.image_build.stale_seconds,
    )
    available_runtime_configs = (
        runtime_configs if runtime_configs is not None else _available_runtime_configs(config)
    )
    runtime_config = _required_runtime_config(
        execution.runtime,
        available_runtime_configs,
    )
    instance_runtime = ContainerInstanceRuntimeResolver()
    runtime = OciRuntimeCommandController(
        runtime_config=runtime_config,
        runtime_configs=available_runtime_configs,
        container_runtime=instance_runtime,
    )
    checkpoint_activity = CheckpointLeaseRegistry()
    source_materializer = SourceCodePackageMaterializer(
        cache_root=_source_cache_root(config),
        cache_max_bytes=configuration.source_cache.max_bytes,
        cache_max_entries=configuration.source_cache.max_entries,
        http=internal_http,
    )
    source_cache_identity = WorkerSourceCacheIdentity.open(
        source_materializer.cache_root,
        storage_id=config.source_cache_storage_id,
    )
    remote_state = RemoteWorkerRepositoryState(worker_id=identity.worker_id)
    worker_repository = RemoteSchedulerWorkerRepository(
        repository,
        remote_state,
        source_cache_identity,
        source_materializer,
    )
    container_repository = RemoteSchedulerContainerRepository(repository, remote_state)
    instance_store: WorkerContainerInstanceStore = LocalWorkerContainerInstanceStore()
    network_backend = _client_network_backend(config, repository)
    registration = _scheduler_worker_record(
        identity,
        config,
        list(available_runtime_configs),
    )
    container_credentials = RemoteWorkerCredentialService(repository)
    credential_hydrator = WorkerCredentialHydrator(
        credentials=container_credentials,
        ssh_identities=RemoteContainerSshIdentitySource(repository),
    )
    event_sink = RemoteWorkerEventSink(repository)
    usage_recorder = RemoteWorkerUsageRecorder(repository)
    log_sink = RemoteSandboxProcessLogSink(repository)
    container_log_capture = WorkerContainerLogCaptureService(RemoteContainerLogSink(repository))
    disk_layers_root, disk_lease_root = disk_layout(paths.disk_root.expanduser().resolve())
    durable_disks = WorkerDurableDiskService(
        engine=DiskEngine(run_root=Path(DEFAULT_DISK_RUN_ROOT)),
        leases=repository,
        layers_root=disk_layers_root,
        lease_root=disk_lease_root,
        mount_root=Path(DEFAULT_DISK_RUN_ROOT) / "mounts",
        volumes=(
            DiskVolumeMounts()
            if configuration.execution.capacity.disk_volume_slots is not None
            else None
        ),
    )
    # Runs before anything attaches. Daemons and devices a previous process left
    # are orphans, and cleanup releases the leases they served.
    durable_disks.recover()
    container_rootfs = ContainerRootfsOverlayManager(
        image_mount_root=Path(paths.image_mount_root),
        scratch_root=paths.container_rootfs_root,
    )
    cache_server = _worker_content_cache(config, internal_http)
    if cache_server is None:
        raise RuntimeError("worker image startup requires a configured content cache")
    image_cache_service = ImageContentCacheService(cache_server)
    image_cache_connection = image_cache_service.start()
    image_runtime_process: ImageRuntimeProcess | None = None
    image_runtime = image_runtime_client
    image_content_cache_root = (
        paths.cache_root.expanduser().resolve()
        if paths.cache_root is not None
        else Path(paths.image_cache_path).expanduser().resolve().parent / "image-content"
    )
    if image_runtime is None:
        image_runtime_process = ImageRuntimeProcess(
            image_root=Path(paths.image_cache_path).expanduser().resolve(),
            mount_root=Path(paths.image_mount_root).expanduser().resolve(),
            cache_root=image_content_cache_root,
            build_root=paths.image_build_root.expanduser().resolve(),
            content_cache=image_cache_connection,
        )
        try:
            image_runtime = image_runtime_process.start()
        except BaseException:
            image_cache_service.close()
            raise
    if image_mounter is None:
        image_mounter = BrokeredClipImageMounter(repository, image_runtime)
    checkpoint_state_sink = RemoteCheckpointStateSink(repository)
    automatic_checkpoint_leases = RemoteAutomaticCheckpointCreationLeaseCoordinator(repository)
    checkpoint_restore_source = RemoteCheckpointRestoreSource(
        repository,
        internal_http,
    )
    checkpoints = RuntimeCheckpointCreator(
        runtime=runtime,
        state_sink=checkpoint_state_sink,
        persister=RemoteCheckpointPersister(
            repository,
            internal_http,
            cache=cache_server,
        ),
        checkpoint_root=paths.checkpoint_root,
        content_cache_available=True,
        checkpoint_activity=checkpoint_activity,
    )
    lifecycle_events = AsyncContainerLifecycleSink(
        RemoteContainerLifecycleSink(repository, worker_id=identity.worker_id)
    )
    runtime_monitor = WorkerContainerRuntimeMonitor(
        metrics=WorkerContainerMetricsService(
            worker_id=identity.worker_id,
            sink=RemoteContainerMetricsSink(repository),
            disk_usage=container_rootfs,
            root_disk=durable_disks,
            network_egress=network_backend.egress_counters,
        ),
        metrics_source_factory=CgroupContainerMetricsSourceFactory(),
        usage_recorder=WorkerSupervisionService(
            worker_id=identity.worker_id,
            event_sink=event_sink,
            usage_recorder=usage_recorder,
            pool_mode=execution.pool_mode,
            billing_owner=execution.billing_owner,
        ),
        # The only thing that keeps a running container's scheduler record
        # alive. Its TTL is re-armed by a write, and nothing else writes after
        # the container is marked running.
        container_states=container_repository,
        settings=ContainerRuntimeMonitorSettings(
            sample_interval_seconds=configuration.monitoring.metrics_interval_seconds
        ),
    )
    image_build_credential_loader = RemoteImageBuildCredentialLoader(repository)
    archive_source_loader = image_source_loader or BrokeredImageArchiveSourceLoader(
        repository,
        internal_http,
    )
    cache_metadata = (
        CacheServerImageArchiveMetadataProvider(cache_server) if cache_server is not None else None
    )
    image_loader = WorkerImageStartupLoader(
        mounter=image_mounter,
        cache=cache_server,
        source_loader=archive_source_loader,
        cache_metadata=cache_metadata,
        image_cache_path=paths.image_cache_path,
        image_mount_root=paths.image_mount_root,
        image_content_cache_root=str(image_content_cache_root / "image-layers"),
        image_archive_extension=config.image_archive_extension,
        storage_mode=ImageArchiveStorageMode.Local,
        publish_source_to_cache=cache_server is not None,
    )
    gpu_assigner = _gpu_assigner(config)
    workspace_storage_mounter = WorkerWorkspaceStorageManager(
        config=WorkspaceStorageConfig(
            base_mount_path=config.workspace_storage_base_mount_path,
            geesefs=WorkspaceGeeseFsStorageConfig(
                binary=config.workspace_storage_geesefs_binary,
                memory_limit_mb=config.workspace_storage_geesefs_memory_limit_mb,
                worker_memory_mib=execution.capacity.memory_mib,
            ),
        ),
    )
    image_archive_publisher = RepositoryWorkerImageArchivePublisher(repository, internal_http)
    request_mounts = mountpoint_backend or WorkerRequestMountManager(
        mountpoint_binary=config.workspace_storage_mountpoint_binary
    )
    sandbox_process_managers = SupervisorSandboxProcessManagerFactory()
    instance_runtime.instances = instance_store
    sandbox_docker = WorkerSandboxDockerService(
        instances=instance_store,
        process_managers=sandbox_process_managers,
    )
    spec_builder = OciRuntimeSpecBuilder(
        bundle_root=paths.bundle_root,
        image_mount_root=Path(paths.image_mount_root),
        runtime_configs=available_runtime_configs,
        gateway_settings=GatewayServiceSettings(
            http=GatewayEndpointSettings(
                host=network_backend.config.gateway,
                port=AGENT_TUNNEL_CONTROL_PORT,
                tls=False,
            )
        ),
        managed_runtime_root=MANAGED_RUNTIME_IMAGE_ROOT,
    )
    dependencies = WorkerProcessExecutionDependencies(
        image_loader=image_loader,
        port_allocator=HostPortAllocator(config.host_port_bind_address),
        mount_preparer=WorkerRequestMountPreparer(
            request_mounts,
            source_materializer,
        ),
        rootfs_preparer=container_rootfs,
        spec_builder=spec_builder,
        runtime_executor=runtime,
        runtime_controller=runtime,
        network_preparer=network_backend,
        workspace_storage_mounter=workspace_storage_mounter,
        gpu_assigner=gpu_assigner,
        instance_recorder=OciContainerServiceInstanceRecorder(
            instances=instance_store,
            identity=identity,
            cache_available=cache_server is not None,
        ),
        credential_hydrator=credential_hydrator,
        checkpoint_restorer=RuntimeCheckpointRestorer(
            source=checkpoint_restore_source,
            state_sink=checkpoint_state_sink,
            runtime=runtime,
            checkpoint_root=paths.checkpoint_root,
            checkpoint_activity=checkpoint_activity,
            cache=cache_server,
        ),
        automatic_checkpoints=WorkerAutomaticCheckpointService(
            instances=instance_store,
            creator=checkpoints,
            leases=automatic_checkpoint_leases,
            runtime=runtime,
        ),
        container_logs=container_log_capture,
        durable_disks=durable_disks,
        lifecycle_events=lifecycle_events,
        runtime_monitor=runtime_monitor,
    )
    container_service_dependencies = WorkerProcessContainerServiceDependencies(
        process_managers=sandbox_process_managers,
        sandbox_docker=sandbox_docker,
        logs=log_sink,
        checkpoints=checkpoints,
        network_policy=network_backend,
    )
    finalization_dependencies = WorkerProcessFinalizationDependencies(
        bundle_root=paths.bundle_root,
        container_ips=network_backend,
        gpu=gpu_assigner,
        network=network_backend,
        request_mounts=request_mounts,
        source_workspaces=source_materializer,
    )
    image_build_dependencies = WorkerProcessImageBuildDependencies(
        image_builder=BuildahWorkerImageBuilder(
            scratch=image_build_scratch,
            repository=repository,
            archive_root=Path(paths.image_cache_path),
            index_cache_root=image_content_cache_root,
            content_cache=image_cache_connection,
            context_loader=RepositoryImageBuildContextLoader(repository, internal_http),
            filesystem_exporter=ContainerFilesystemExporter(instance_store, runtime),
        ),
        image_archive_publisher=image_archive_publisher,
        image_build_credential_loader=image_build_credential_loader,
    )
    retention = WorkerRetentionService(
        instances=instance_store,
        config=WorkerRetentionConfig(
            image_cache_root=Path(paths.image_cache_path),
            image_mount_root=Path(paths.image_mount_root),
            image_layer_cache_root=image_content_cache_root / "image-layers",
            checkpoint_root=Path(paths.checkpoint_root),
            image_archive_extension=config.image_archive_extension,
            image_cache_max_bytes=config.image_cache_max_bytes,
            image_materialization_max_bytes=config.image_materialization_max_bytes,
            checkpoint_cache_max_bytes=config.checkpoint_cache_max_bytes,
            low_watermark_pct=config.retention_low_watermark_pct,
            recent_guard_seconds=config.retention_recent_guard_seconds,
            materialization_retention_seconds=config.image_materialization_retention_seconds,
            checkpoint_retention_seconds=config.checkpoint_retention_seconds,
            cache_pruning_enabled=config.retention_enabled,
        ),
        image_build_scratch=image_build_scratch,
        checkpoint_activity=checkpoint_activity,
        image_unmounter=image_runtime.unmount,
    )
    readiness_preparer, readiness_validator = _readiness_steps(
        image_runtime,
        spec_builder=spec_builder,
        network_backend=network_backend,
        readiness=readiness,
        gpu_count=execution.capacity.gpu_count,
        gpu_devices=config.gpu_devices,
    )
    return assemble_worker_process_services(
        identity=identity,
        dependencies=dependencies,
        workers=worker_repository,
        containers=container_repository,
        instances=instance_store,
        event_sink=event_sink,
        usage_recorder=usage_recorder,
        event_source=repository,
        pool_mode=execution.pool_mode,
        billing_owner=execution.billing_owner,
        registration=registration,
        readiness_preparer=readiness_preparer,
        readiness_validator=readiness_validator,
        cleanup_actions=(
            [WorkerCleanupAction(name="prepared-networks", action=network_backend.close)]
            + (
                [
                    WorkerCleanupAction(
                        name="image-runtime",
                        action=image_runtime_process.stop,
                    )
                ]
                if image_runtime_process is not None
                else []
            )
            + [WorkerCleanupAction(name="image-content-cache", action=image_cache_service.close)]
        ),
        container_service_dependencies=container_service_dependencies,
        finalization_dependencies=finalization_dependencies,
        image_build_dependencies=image_build_dependencies,
        source_cache_reconciler=worker_repository,
        retention=retention,
        credential_refresher=WorkspaceCredentialRefresher(
            storage=workspace_storage_mounter,
            instances=instance_store,
            credentials=container_credentials,
            principal=WorkerCredentialPrincipal(
                workspace_id="",
                token_kind=TokenKind.Worker,
            ),
        ),
    )


def _readiness_steps(
    image_runtime: ImageRuntimeClient | None,
    *,
    spec_builder: OciRuntimeSpecBuilder,
    network_backend: AgentBridgeNetworkBackend,
    readiness: Future[None],
    gpu_count: int,
    gpu_devices: str,
) -> tuple[Callable[[], None], Callable[[], None]]:
    """The worker's readiness work: local work beside registration, then the rest after it."""
    return (
        lambda: _prepare_worker_readiness(
            image_runtime, spec_builder=spec_builder, readiness=readiness
        ),
        lambda: _validate_worker_readiness(
            network_backend=network_backend, gpu_count=gpu_count, gpu_devices=gpu_devices
        ),
    )


def _prepare_worker_readiness(
    image_runtime: ImageRuntimeClient | None,
    *,
    spec_builder: OciRuntimeSpecBuilder,
    readiness: Future[None],
) -> None:
    """The local readiness work, run while the worker registers.

    A reserve's worker is held off the control plane until its machine resumes,
    so nothing here may call it: the host network waits for the network lock the
    control plane grants, and runs after registration.
    """

    def image_runtime_health() -> None:
        if image_runtime is None:
            return
        response = image_runtime.health()
        if not response.ok:
            raise RuntimeError(response.error or "image runtime is unavailable")

    try:
        _run_readiness_checks(
            "container worker readiness preparation",
            {
                "managed_runtimes": spec_builder.prepare_managed_runtimes,
                "image_runtime": image_runtime_health,
            },
        )
    except BaseException as exc:
        readiness.set_exception(exc)
        raise
    readiness.set_result(None)


def _validate_worker_readiness(
    *,
    network_backend: AgentBridgeNetworkBackend,
    gpu_count: int,
    gpu_devices: str,
) -> None:
    """The readiness work that needs the control plane or sees what a sleep changes.

    A reserve's worker registers only after its machine resumes, so this sets up
    the host network under the control plane's lock and sees the machine as it
    woke: its addressing and routes, and its GPUs.
    """

    def gpu_presence() -> None:
        # A driver may take a moment to answer after the machine wakes.
        deadline = time.monotonic() + GPU_PRESENCE_WAIT_SECONDS
        gpus = NvidiaGpuIndexProvider(visible_devices=gpu_devices or "all")
        while True:
            try:
                found = len(gpus.query_devices())
                if found >= gpu_count:
                    return
                failure = RuntimeError(f"nvidia-smi lists {found} of the worker's {gpu_count} GPUs")
            except RuntimeError as exc:
                failure = exc
            if time.monotonic() >= deadline:
                raise failure
            time.sleep(0.5)

    def network() -> None:
        network_backend.prepare()
        network_backend.probe_gateway_egress()

    checks: dict[str, Callable[[], object]] = {"network": network}
    if gpu_count:
        checks["gpu"] = gpu_presence
    _run_readiness_checks("container worker readiness checks", checks)


def _run_readiness_checks(label: str, checks: dict[str, Callable[[], object]]) -> None:
    """Run readiness checks side by side, since none depends on another.

    Each still has to pass; the first failure is raised once all have finished.
    """
    timings = StepTimings()

    def timed(name: str) -> None:
        with timings.step(name):
            checks[name]()

    try:
        with ThreadPoolExecutor(
            max_workers=len(checks), thread_name_prefix="worker-readiness"
        ) as pool:
            outcomes = {name: pool.submit(timed, name) for name in checks}
        failures = [
            (name, error)
            for name, outcome in outcomes.items()
            if (error := outcome.exception()) is not None
        ]
        # The first failure is raised; the rest are logged so they are not hidden.
        for name, error in failures[1:]:
            LOGGER.error("container worker readiness check %s failed", name, exc_info=error)
        if failures:
            raise failures[0][1]
    finally:
        timings.log(LOGGER, label)


def planned_scheduler_worker_record_from_settings(
    config: WorkerSettings,
) -> SchedulerWorkerRecord:
    return _scheduler_worker_record(
        _worker_identity(config),
        config,
        [config.configuration.execution.runtime],
    )


def _internal_http_client(config: WorkerSettings) -> InternalHttpClient:
    return InternalHttpClient(timeout_seconds=config.worker_repository_timeout_seconds)


def _worker_content_cache(
    config: WorkerSettings,
    internal_http: InternalHttpClient,
) -> FileCacheServer | WorkerCacheHttpClient | None:
    if config.cache_endpoint:
        return WorkerCacheHttpClient(
            WorkerCacheEndpoint(url=config.cache_endpoint),
            service_token=_cache_service_token(config),
            http=internal_http,
        )
    cache_root = config.configuration.paths.cache_root
    if cache_root is not None:
        return FileCacheServer(cache_root)
    return None


def _cache_service_token(config: WorkerSettings) -> str:
    if config.cache_service_token:
        return config.cache_service_token
    if config.cache_service_token_file is not None:
        token = config.cache_service_token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    msg = "cache endpoint requires a configured cache service token or token file"
    raise ValueError(msg)


def _source_cache_root(config: WorkerSettings) -> Path:
    """Where source packages are cached, derived from the content cache when unset.

    Colocating the two keeps a worker's caches on the one volume the launcher
    sized for them; only a configuration that names neither falls back to the
    package default.
    """
    paths = config.configuration.paths
    if paths.source_cache_root is not None:
        return paths.source_cache_root.expanduser().resolve()
    if paths.cache_root is not None:
        return (paths.cache_root / "source-code").expanduser().resolve()
    return DEFAULT_SOURCE_CACHE_ROOT


def _worker_identity(config: WorkerSettings) -> WorkerRouteIdentity:
    if not config.worker_id:
        msg = "worker id is required"
        raise ValueError(msg)
    execution = config.configuration.execution
    return WorkerRouteIdentity(
        worker_id=config.worker_id,
        placement=config.placement,
        machine_id=config.machine_id,
        pod_address=config.pod_address,
        container_service_port=config.container_service_port,
        persistent=execution.persistent,
    )


def _scheduler_worker_record(
    identity: WorkerRouteIdentity,
    config: WorkerSettings,
    runtime_classes: list[OciRuntimeName],
) -> SchedulerWorkerRecord:
    execution = config.configuration.execution
    capacity = execution.capacity
    volume_slots = capacity.disk_volume_slots
    disk_bytes = _disk_capacity_bytes(config) if volume_slots is None else 0
    return SchedulerWorkerRecord(
        worker_id=identity.worker_id,
        runtime_image=config.runtime_image,
        agent_binary_sha256=config.agent_binary_sha256,
        placement=identity.placement,
        capacity_owner_id=_required_capacity_owner_id(config),
        machine_id=identity.machine_id,
        status=SchedulerWorkerStatus.Pending,
        gpu_type=capacity.gpu_type,
        runtime_class=execution.runtime.value,
        runtime_classes=[runtime.value for runtime in runtime_classes],
        private_worker=identity.placement.kind is not PlacementKind.Platform,
        preemptible=execution.preemptible,
        free_cpu_millicores=capacity.cpu_millicores,
        free_memory_mib=capacity.memory_mib,
        free_gpu_count=capacity.gpu_count,
        free_disk_bytes=disk_bytes,
        free_disk_volumes=volume_slots or 0,
        total_cpu_millicores=capacity.cpu_millicores,
        total_memory_mib=capacity.memory_mib,
        total_gpu_count=capacity.gpu_count,
        total_disk_bytes=disk_bytes,
        total_disk_volumes=volume_slots or 0,
        disk_storage=DiskStorage.Host if volume_slots is None else DiskStorage.Volume,
    )


def _disk_capacity_bytes(config: WorkerSettings) -> int:
    root = config.configuration.paths.disk_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return disk_capacity_bytes(shutil.disk_usage(root).total)


def _required_capacity_owner_id(config: WorkerSettings) -> str:
    capacity_owner_id = config.capacity_owner_id.strip()
    if not capacity_owner_id:
        raise RuntimeError("worker capacity owner id is required")
    return capacity_owner_id


def _available_runtime_configs(
    config: WorkerSettings,
) -> dict[OciRuntimeName, RuntimeBinaryConfig]:
    available: dict[OciRuntimeName, RuntimeBinaryConfig] = {}
    availability_by_runtime: dict[OciRuntimeName, RuntimeAvailability] = {}
    for runtime in config.configuration.execution.runtimes:
        runtime_config = RuntimeBinaryConfig(runtime=runtime)
        availability = runtime_availability(
            runtime_config,
            verify=_verify_runtime_binary,
        )
        availability_by_runtime[runtime] = availability
        if availability.available:
            available[runtime] = runtime_config
    default_availability = availability_by_runtime[config.configuration.execution.runtime]
    if not default_availability.available:
        raise RuntimeUnavailableError(default_availability)
    return available


def _required_runtime_config(
    runtime: OciRuntimeName,
    runtime_configs: dict[OciRuntimeName, RuntimeBinaryConfig],
) -> RuntimeBinaryConfig:
    config = runtime_configs.get(runtime)
    if config is not None:
        return config
    raise RuntimeUnavailableError(
        RuntimeAvailability(
            runtime=runtime,
            status=RuntimeAvailabilityStatus.Missing,
            binary_path=runtime.value,
            reason="runtime was not enabled for this worker",
        )
    )


def _verify_runtime_binary(path: str) -> str | None:
    try:
        result = run_process(
            [path, "--version"],
            timeout_seconds=RUNTIME_VERSION_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, ProcessTimeoutError) as exc:
        return f"version probe could not execute: {exc}"
    if result.ok:
        return None
    detail = (result.stderr or result.stdout).strip()
    if detail:
        return f"version probe exited {result.exit_code}: {detail[:512]}"
    return f"version probe exited {result.exit_code} without diagnostic output"


def _client_network_backend(
    config: WorkerSettings,
    client: WorkerRepositoryHttpClient,
) -> AgentBridgeNetworkBackend:
    network = config.configuration.network
    bridge = AgentBridgeNetworkConfig(
        bridge_name=network.bridge_name,
        subnet=network.bridge_subnet,
        ipv6_subnet=network.bridge_ipv6_subnet,
    )
    return AgentBridgeNetworkBackend(
        SchedulerNetworkIpAllocator(
            RemoteWorkerNetworkIpRepository(client),
            # The control plane scopes every network mutation by the worker's own
            # record; this prefix only names the network in local state and logs.
            network_prefix=config.network_prefix or config.placement.key or config.worker_id,
            # The allocator issues addresses onto this bridge, so it has to be told
            # which one: left on the default it would hand out addresses the bridge
            # does not front.
            subnet=bridge.subnet,
            worker_id=config.worker_id,
        ),
        config=bridge,
        egress_counters=WorkerNetworkEgressCounters(
            load_policy=client.egress_policy,
            worker_id=config.worker_id,
            event_sink=RemoteWorkerEventSink(client),
        ),
    )


def _gpu_assigner(config: WorkerSettings) -> WorkerGpuRuntimeAssigner:
    indices = _gpu_device_indices(config.gpu_devices)
    allocation = (
        GpuAllocationManager(indices)
        if indices
        else DynamicGpuAllocationManager(
            NvidiaGpuIndexProvider(
                visible_devices=config.gpu_devices or "all",
            )
        )
    )
    return WorkerGpuRuntimeAssigner(
        allocation=allocation,
        cdi_enabled=config.nvidia_cdi_enabled,
    )


def _gpu_device_indices(value: str) -> list[int]:
    indices: list[int] = []
    for token in value.split(","):
        candidate = token.strip()
        if candidate.isdigit():
            indices.append(int(candidate))
    return sorted(dict.fromkeys(indices))
