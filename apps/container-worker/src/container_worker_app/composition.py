from __future__ import annotations

import socket
from pathlib import Path
from urllib.parse import urlparse

from cache.server import (
    FileCacheServer,
    WorkerCacheEndpoint,
    WorkerCacheHttpClient,
)
from foundation.process import ProcessTimeoutError, run_process
from networking.agent_peer_client import AgentPeerClient
from networking.internal_http import (
    InternalHttpClient,
    TailnetHostPolicy,
    TailnetPeerAddresses,
)
from shared.identity import TokenKind
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from worker.adapters import WorkerRouteIdentity
from worker.automatic_checkpoints import WorkerAutomaticCheckpointService
from worker.cache_assets import (
    WorkspaceGeeseFsStorageConfig,
    WorkspaceStorageConfig,
)
from worker.checkpoint_activity import CheckpointLeaseRegistry
from worker.checkpoint_restore import RuntimeCheckpointRestorer
from worker.container_checkpoints import (
    ContainerFilesystemArchiveCreator,
    RuntimeCheckpointCreator,
    TarContainerImageArchiver,
)
from worker.container_logs import WorkerContainerLogCaptureService
from worker.container_metrics import (
    ProcessTreeContainerMetricsSourceFactory,
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
from worker.events import WorkerPoolMode
from worker.execution import (
    GatewayEndpointSettings,
    GatewayServiceSettings,
)
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
from worker.workspace_credential_refresh import WorkspaceCredentialRefresher
from worker.workspace_storage import WorkerWorkspaceStorageManager

from .checkpoint_transfer import RemoteCheckpointPersister, RemoteCheckpointRestoreSource
from .container_instances import (
    ContainerInstanceRuntimeResolver,
    OciContainerServiceInstanceRecorder,
)
from .image_archives import (
    BrokeredImageArchiveSourceLoader,
    CacheServerImageArchiveMetadataProvider,
    TarImageArchiveMounter,
)
from .process_assembly import (
    WorkerProcessContainerServiceDependencies,
    WorkerProcessExecutionDependencies,
    WorkerProcessFinalizationDependencies,
    WorkerProcessImageBuildDependencies,
    WorkerProcessServices,
    assemble_worker_process_services,
)
from .settings import WorkerSettings

RUNTIME_VERSION_PROBE_TIMEOUT_SECONDS = 5.0


def build_worker_process_services(
    *,
    settings: WorkerSettings,
    image_mounter: WorkerImageArchiveMounter | None = None,
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
    repository = repository_client or build_worker_repository_http_client(
        endpoint=config.worker_repository_endpoint,
        token=config.worker_token,
        timeout_seconds=config.worker_repository_timeout_seconds,
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
    credential_hydrator = WorkerCredentialHydrator(credentials=container_credentials)
    event_sink = RemoteWorkerEventSink(repository)
    usage_recorder = RemoteWorkerUsageRecorder(repository)
    log_sink = RemoteSandboxProcessLogSink(repository)
    container_log_capture = WorkerContainerLogCaptureService(RemoteContainerLogSink(repository))
    container_rootfs = ContainerRootfsOverlayManager(
        image_mount_root=Path(paths.image_mount_root),
        scratch_root=paths.container_rootfs_root,
    )
    cache_server = _worker_content_cache(config, internal_http)
    checkpoint_state_sink = RemoteCheckpointStateSink(repository)
    automatic_checkpoint_leases = RemoteAutomaticCheckpointCreationLeaseCoordinator(repository)
    checkpoint_restore_source = RemoteCheckpointRestoreSource(
        repository,
        internal_http,
        checkpoint_bucket=config.checkpoint_bucket,
    )
    checkpoints = RuntimeCheckpointCreator(
        runtime=runtime,
        state_sink=checkpoint_state_sink,
        persister=RemoteCheckpointPersister(
            repository,
            internal_http,
            checkpoint_bucket=config.checkpoint_bucket,
            cache_namespace=config.checkpoint_cache_namespace,
            cache=cache_server,
        ),
        checkpoint_root=paths.checkpoint_root,
        origin_storage_available=bool(config.checkpoint_bucket),
        content_cache_available=True,
        checkpoint_activity=checkpoint_activity,
    )
    lifecycle_events = AsyncContainerLifecycleSink(
        RemoteContainerLifecycleSink(repository, worker_id=identity.worker_id)
    )
    metrics_enabled = configuration.monitoring.metrics_enabled
    runtime_monitor = WorkerContainerRuntimeMonitor(
        metrics=(
            WorkerContainerMetricsService(
                worker_id=identity.worker_id,
                sink=RemoteContainerMetricsSink(repository),
                disk_usage=container_rootfs,
            )
            if metrics_enabled
            else None
        ),
        metrics_source_factory=(
            ProcessTreeContainerMetricsSourceFactory() if metrics_enabled else None
        ),
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
        mounter=image_mounter or TarImageArchiveMounter(),
        cache=cache_server,
        source_loader=archive_source_loader,
        cache_metadata=cache_metadata,
        image_cache_path=paths.image_cache_path,
        image_mount_root=paths.image_mount_root,
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
    image_archiver = TarContainerImageArchiver(
        target_root=Path(paths.image_cache_path),
        extension=config.image_archive_extension,
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
    dependencies = WorkerProcessExecutionDependencies(
        image_loader=image_loader,
        port_allocator=HostPortAllocator(config.host_port_bind_address),
        mount_preparer=WorkerRequestMountPreparer(
            request_mounts,
            source_materializer,
        ),
        rootfs_preparer=container_rootfs,
        spec_builder=OciRuntimeSpecBuilder(
            bundle_root=paths.bundle_root,
            image_mount_root=Path(paths.image_mount_root),
            runtime_configs=available_runtime_configs,
            gateway_settings=_gateway_settings(config),
            managed_runtime_root=MANAGED_RUNTIME_IMAGE_ROOT,
        ),
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
        lifecycle_events=lifecycle_events,
        runtime_monitor=runtime_monitor,
    )
    container_service_dependencies = WorkerProcessContainerServiceDependencies(
        process_managers=sandbox_process_managers,
        sandbox_docker=sandbox_docker,
        logs=log_sink,
        checkpoints=checkpoints,
        archives=ContainerFilesystemArchiveCreator(
            runtime=runtime,
            archiver=image_archiver,
            publisher=image_archive_publisher,
        ),
        network_policy=network_backend,
        port_exposer=network_backend,
    )
    finalization_dependencies = WorkerProcessFinalizationDependencies(
        container_ips=network_backend,
        gpu=gpu_assigner,
        network=network_backend,
        request_mounts=request_mounts,
        source_workspaces=source_materializer,
    )
    image_build_dependencies = WorkerProcessImageBuildDependencies(
        image_builder=BuildahWorkerImageBuilder(
            archiver=image_archiver,
            scratch=image_build_scratch,
            context_loader=RepositoryImageBuildContextLoader(repository, internal_http),
        ),
        image_archive_publisher=image_archive_publisher,
        image_build_credential_loader=image_build_credential_loader,
    )
    retention = WorkerRetentionService(
        instances=instance_store,
        config=WorkerRetentionConfig(
            image_cache_root=Path(paths.image_cache_path),
            image_mount_root=Path(paths.image_mount_root),
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
        readiness_validator=(
            None
            if network_backend is None
            else lambda: _initialize_network_backend(
                network_backend,
                _gateway_runtime_network_endpoint(config),
            )
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


def planned_scheduler_worker_record_from_settings(
    config: WorkerSettings,
) -> SchedulerWorkerRecord:
    return _scheduler_worker_record(
        _worker_identity(config),
        config,
        [config.configuration.execution.runtime],
    )


def _internal_http_client(config: WorkerSettings) -> InternalHttpClient:
    """The worker's one HTTP client, routed when the node runs a tailnet.

    The worker holds no tailnet client of its own; the agent that launched it
    answers peer lookups over loopback. Without both an address and a suffix the
    client dials names as written, which is what a single-host Compose stack
    wants and needs no branch anywhere else.
    """
    timeout = config.worker_repository_timeout_seconds
    if not config.peer_resolver_address or not config.tailnet_dns_suffix:
        return InternalHttpClient(timeout_seconds=timeout)
    return InternalHttpClient(
        timeout_seconds=timeout,
        addresses=TailnetPeerAddresses(
            runtime=AgentPeerClient(
                agent_address=config.peer_resolver_address,
                worker_token=config.worker_token,
            ),
            policy=TailnetHostPolicy(dns_suffix=config.tailnet_dns_suffix),
        ),
    )


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
        pool=config.pool,
        machine_id=config.machine_id,
        pod_address=config.pod_address,
        container_service_port=config.container_service_port,
        persistent=execution.persistent,
        route_transport=config.configuration.network.route_transport,
        route_local_target_host=config.route_local_target_host,
        agent_worker=execution.agent_worker,
    )


def _gateway_settings(config: WorkerSettings) -> GatewayServiceSettings:
    gateway_url = _gateway_runtime_network_endpoint(config)
    if gateway_url and "://" not in gateway_url:
        gateway_url = f"http://{gateway_url}"
    parsed = urlparse(gateway_url)
    if not parsed.hostname:
        return GatewayServiceSettings()
    port = parsed.port
    tls = parsed.scheme == "https"
    return GatewayServiceSettings(
        http=GatewayEndpointSettings(
            host=parsed.hostname,
            port=port or (443 if tls else 80),
            tls=tls,
        )
    )


def _gateway_runtime_network_endpoint(config: WorkerSettings) -> str:
    endpoint = config.gateway_runtime_http_endpoint
    parsed = urlparse(endpoint)
    route_target = config.route_local_target_host.strip().lower()
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or parsed.hostname.lower() != route_target
    ):
        return endpoint
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or 80,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        msg = f"worker runtime gateway host {parsed.hostname!r} could not be resolved"
        raise RuntimeError(msg) from exc
    if not addresses:
        msg = f"worker runtime gateway host {parsed.hostname!r} did not resolve to an address"
        raise RuntimeError(msg)
    address = str(addresses[0][4][0])
    host = f"[{address}]" if ":" in address else address
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return parsed._replace(netloc=netloc).geturl()


def _scheduler_worker_record(
    identity: WorkerRouteIdentity,
    config: WorkerSettings,
    runtime_classes: list[OciRuntimeName],
) -> SchedulerWorkerRecord:
    execution = config.configuration.execution
    capacity = execution.capacity
    return SchedulerWorkerRecord(
        worker_id=identity.worker_id,
        pool=identity.pool,
        capacity_owner_id=_required_capacity_owner_id(config),
        machine_id=identity.machine_id,
        status=SchedulerWorkerStatus.Pending,
        gpu_type=capacity.gpu_type,
        runtime_class=execution.runtime.value,
        runtime_classes=[runtime.value for runtime in runtime_classes],
        private_worker=execution.pool_mode is WorkerPoolMode.Private,
        preemptible=execution.preemptible,
        free_cpu_millicores=capacity.cpu_millicores,
        free_memory_mib=capacity.memory_mib,
        free_gpu_count=capacity.gpu_count,
        total_cpu_millicores=capacity.cpu_millicores,
        total_memory_mib=capacity.memory_mib,
        total_gpu_count=capacity.gpu_count,
    )


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
) -> AgentBridgeNetworkBackend | None:
    network = config.configuration.network
    if not network.agent_bridge_network:
        return None
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
            network_prefix=config.network_prefix or config.pool or config.worker_id,
            # The allocator issues addresses onto this bridge, so it has to be told
            # which one: left on the default it would hand out addresses the bridge
            # does not front.
            subnet=bridge.subnet,
            worker_id=config.worker_id,
        ),
        config=bridge,
    )


def _initialize_network_backend(
    backend: AgentBridgeNetworkBackend,
    gateway_public_http_url: str,
) -> None:
    backend.initialize(gateway_public_http_url)


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
