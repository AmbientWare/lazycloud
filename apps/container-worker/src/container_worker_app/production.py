from __future__ import annotations

import hashlib
import http.client
import os
import posixpath
import re
import shutil
import socket
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from cache.server import (
    CacheUnavailableError,
    FileCacheServer,
    WorkerCacheEndpoint,
    WorkerCacheHttpClient,
)
from foundation.process import ProcessTimeoutError, run_process
from pydantic import AliasChoices, Field, JsonValue, TypeAdapter, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from shared.app_identity import (
    NAME,
    OBJECT_STORE_ACCESS_KEY_ID,
    OBJECT_STORE_BUCKET,
    OBJECT_STORE_SECRET_ACCESS_KEY,
)
from shared.capacity import CAPACITY_OWNER_ID_PATTERN
from shared.checkpoints import CheckpointRecord
from shared.env import GATEWAY_HTTP_URL_ENV, WORKER_REPOSITORY_URL_ENV
from shared.routing import BackendRouteTransport
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from storage_client.mounts import (
    StorageMountMode,
)
from worker.adapters import (
    WorkerRouteIdentity,
)
from worker.artifact_retention import (
    DEFAULT_WORKER_CHECKPOINT_CACHE_MAX_BYTES,
    DEFAULT_WORKER_IMAGE_CACHE_MAX_BYTES,
    DEFAULT_WORKER_IMAGE_MATERIALIZATION_MAX_BYTES,
    WorkerArtifactRetentionConfig,
    WorkerArtifactRetentionService,
)
from worker.automatic_checkpoints import WorkerAutomaticCheckpointService
from worker.cache_assets import (
    WorkerStorageMode,
    WorkspaceJuiceFsStorageConfig,
    WorkspaceMountPointStorageConfig,
    WorkspaceStorageConfig,
)
from worker.checkpoint_activity import CheckpointArtifactLeaseRegistry
from worker.checkpoint_restore import RuntimeCheckpointRestorer
from worker.checkpoints import (
    CheckpointPersistencePlan,
)
from worker.configuration import (
    DEFAULT_WORKER_CONFIG_PATH,
    WORKER_CONFIG_PATH_ENV,
    WorkerConfiguration,
)
from worker.container_artifacts import (
    ContainerFilesystemArchiveCreator,
    RuntimeCheckpointCreator,
    TarContainerImageArchiver,
    WorkerCheckpointPersistenceResult,
)
from worker.container_execution import (
    ContainerExecutionContext,
    ContainerMountSetupResult,
    ContainerNetworkSetupResult,
)
from worker.container_logs import WorkerContainerLogCaptureService
from worker.container_metrics import (
    ProcessTreeContainerMetricsSourceFactory,
    WorkerContainerMetricsService,
)
from worker.container_service.models import (
    SandboxDockerDaemonStatus,
    WorkerContainerServiceInstance,
)
from worker.container_service.protocols import WorkerContainerInstanceStore
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.container_service.supervisor_process_manager import (
    SupervisorSandboxProcessManagerFactory,
)
from worker.container_startup import (
    IMAGE_MOUNT_MANIFEST_NAME,
    HostPortAllocator,
    ImageMountManifest,
    WorkerImageArchiveCacheMetadata,
    WorkerImageArchiveMounter,
    WorkerImageArchiveSourceLoader,
    WorkerImageMountRequest,
    WorkerImageMountResult,
    WorkerImageMountStatus,
    WorkerImageSourceLoadRequest,
    WorkerImageSourceLoadResult,
    WorkerImageStartupLoader,
    WorkerRequestMountPreparer,
)
from worker.credential_hydration import WorkerCredentialHydrator
from worker.events import WorkerPoolMode
from worker.execution import (
    GatewayEndpointSettings,
    GatewayServiceSettings,
    PortBinding,
    container_port_address_map,
    select_container_network,
)
from worker.gpu import (
    DynamicGpuAllocationManager,
    GpuAllocationManager,
    NvidiaGpuIndexProvider,
    WorkerGpuRuntimeAssigner,
)
from worker.image_archive_transfer import download_image_archive
from worker.image_build_execution import (
    BuildahWorkerImageBuilder,
    RepositoryImageBuildContextLoader,
    RepositoryWorkerImageArchivePublisher,
)
from worker.image_build_runtime_credentials import RemoteImageBuildCredentialLoader
from worker.image_build_scratch import ImageBuildScratchManager
from worker.image_lifecycle import (
    DEFAULT_IMAGE_ARCHIVE_EXTENSION,
    ImageArchiveStorageMode,
)
from worker.managed_runtime import MANAGED_RUNTIME_ARTIFACT_ROOT
from worker.monitoring import (
    AsyncContainerLifecycleSink,
    ContainerRuntimeMonitorSettings,
    WorkerContainerRuntimeMonitor,
)
from worker.network_backend import (
    AgentBridgeNetworkBackend,
    SchedulerNetworkIpAllocator,
)
from worker.oci_runtime import (
    OciRuntimeCommandController,
    OciRuntimeSpecBuilder,
)
from worker.oci_spec import OciRuntimeContainerSpec
from worker.origin_access import CacheOriginCredentialRequest
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
from worker.repository_payloads import (
    GetCheckpointRestoreRequest,
    PersistCheckpointArchiveRequest,
    PrepareCheckpointArchiveUploadRequest,
)
from worker.request_mounts import WorkerRequestMountLifecycle, WorkerRequestMountManager
from worker.runtime_config import (
    OciRuntimeName,
    RuntimeAvailability,
    RuntimeAvailabilityStatus,
    RuntimeBinaryConfig,
    RuntimeUnavailableError,
    runtime_availability,
)
from worker.sandbox_docker import WorkerSandboxDockerService
from worker.sandbox_server import SandboxContainerMount
from worker.source_cache_cleanup import WorkerSourceCacheIdentity
from worker.source_code import DEFAULT_SOURCE_CACHE_ROOT, SourceCodePackageMaterializer
from worker.status import DEFAULT_WORKER_SPINDOWN_SECONDS
from worker.supervision import (
    HttpWorkerContainerCostResolver,
    WorkerContainerCostResolver,
    WorkerSupervisionService,
)
from worker.workspace_storage import WorkerWorkspaceStorageManager

from .process_assembly import (
    WorkerProcessContainerServiceDependencies,
    WorkerProcessExecutionDependencies,
    WorkerProcessFinalizationDependencies,
    WorkerProcessImageBuildDependencies,
    WorkerProcessServices,
    build_worker_process_services,
)

DEFAULT_CHECKPOINT_CACHE_NAMESPACE = "checkpoints"
RUNTIME_VERSION_PROBE_TIMEOUT_SECONDS = 5.0
PRESIGNED_DOWNLOAD_CHUNK_SIZE_BYTES = 1024 * 1024

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


@dataclass(slots=True)
class ContainerInstanceRuntimeResolver:
    instances: WorkerContainerInstanceStore | None = None

    def __call__(self, container_id: str) -> OciRuntimeName | None:
        if self.instances is None:
            return None
        instance = self.instances.get_container_instance(container_id)
        return instance.runtime if instance is not None else None


class ProductionWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    configuration: WorkerConfiguration = Field(default_factory=WorkerConfiguration)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del cls, dotenv_settings
        config_path = os.environ.get(WORKER_CONFIG_PATH_ENV, DEFAULT_WORKER_CONFIG_PATH).strip()
        yaml_settings = YamlConfigSettingsSource(
            settings_cls,
            yaml_file=config_path or None,
        )
        return init_settings, env_settings, yaml_settings, file_secret_settings

    worker_id: str = Field(
        default="",
        validation_alias="WORKER_ID",
    )
    worker_token: str = Field(
        default="",
        validation_alias="WORKER_TOKEN",
    )
    worker_repository_url: str = Field(
        default="",
        validation_alias=WORKER_REPOSITORY_URL_ENV,
    )
    gateway_runtime_http_url: str = Field(
        default="",
        validation_alias=GATEWAY_HTTP_URL_ENV,
    )
    worker_repository_timeout_seconds: float = Field(
        default=30.0,
        validation_alias="WORKER_REPOSITORY_TIMEOUT_SECONDS",
    )
    pool_name: str = Field(
        default="default",
        validation_alias="WORKER_POOL",
    )
    capacity_owner_id: str = Field(
        default="",
        validation_alias="WORKER_CAPACITY_OWNER_ID",
    )
    machine_id: str = Field(
        default="",
        validation_alias="WORKER_MACHINE",
    )
    pod_address: str = Field(
        default="",
        validation_alias=AliasChoices(
            "WORKER_POD_ADDRESS",
            "WORKER_POD_IP",
            "WORKER_POD_HOST",
        ),
    )
    container_service_port: int = Field(
        default=0,
        validation_alias="WORKER_CONTAINER_SERVICE_PORT",
    )
    container_service_token: str = Field(
        default="",
        validation_alias="WORKER_CONTAINER_SERVICE_TOKEN",
    )
    runtime: OciRuntimeName | None = Field(
        default=None,
        validation_alias="WORKER_RUNTIME",
    )
    cpu_millicores: int | None = Field(
        default=None,
        validation_alias="WORKER_CPU_MILLICORES",
    )
    memory_mib: int | None = Field(
        default=None,
        validation_alias="WORKER_MEMORY_MIB",
    )
    gpu_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WORKER_GPU", "WORKER_GPU_TYPE"),
    )
    gpu_count: int | None = Field(
        default=None,
        validation_alias="WORKER_GPU_COUNT",
    )
    requires_pool_selector: bool | None = Field(
        default=None,
        validation_alias="WORKER_REQUIRES_POOL_SELECTOR",
    )
    preemptible: bool | None = Field(
        default=None,
        validation_alias="WORKER_PREEMPTIBLE",
    )
    persistent: bool | None = Field(
        default=None,
        validation_alias="WORKER_PERSISTENT",
    )
    worker_spindown_seconds: float = Field(
        default=DEFAULT_WORKER_SPINDOWN_SECONDS,
        validation_alias="WORKER_SPINDOWN_SECONDS",
    )
    agent_worker: bool | None = Field(
        default=None,
        validation_alias="WORKER_AGENT_WORKER",
    )
    route_transport: BackendRouteTransport | None = Field(
        default=None,
        validation_alias="WORKER_ROUTE_TRANSPORT",
    )
    route_local_target_host: str = Field(
        default="",
        validation_alias="WORKER_ROUTE_TARGET",
    )
    agent_bridge_network: bool | None = Field(
        default=None,
        validation_alias="WORKER_AGENT_BRIDGE_NETWORK",
    )
    network_prefix: str | None = Field(
        default=None,
        validation_alias="WORKER_NETWORK_PREFIX",
    )
    host_port_bind_address: str = Field(
        default="",
        validation_alias="WORKER_HOST_PORT_BIND_ADDRESS",
    )
    bundle_root: Path | None = Field(
        default=None,
        validation_alias="WORKER_BUNDLE_ROOT",
    )
    image_cache_path: str | None = Field(
        default=None,
        validation_alias="WORKER_IMAGE_CACHE_PATH",
    )
    image_mount_root: str | None = Field(
        default=None,
        validation_alias="WORKER_IMAGE_MOUNT_ROOT",
    )
    source_cache_root: Path | None = Field(
        default=None,
        validation_alias="WORKER_SOURCE_CACHE_ROOT",
    )
    source_cache_storage_id: str = Field(
        default="",
        validation_alias="WORKER_SOURCE_CACHE_STORAGE_ID",
    )
    image_archive_extension: str = Field(
        default=DEFAULT_IMAGE_ARCHIVE_EXTENSION,
        validation_alias="WORKER_IMAGE_ARCHIVE_EXTENSION",
    )
    cache_root: Path | None = Field(
        default=None,
        validation_alias="WORKER_CACHE_ROOT",
    )
    cache_endpoint: str = Field(
        default="",
        validation_alias="WORKER_CACHE_ENDPOINT",
    )
    cache_service_token: str = Field(
        default="",
        validation_alias="LAZYCLOUD_CACHE_SERVICE_TOKEN",
    )
    cache_service_token_file: Path | None = Field(
        default=None,
        validation_alias="LAZYCLOUD_CACHE_SERVICE_TOKEN_FILE",
    )
    checkpoint_root: str | None = Field(
        default=None,
        validation_alias="WORKER_CHECKPOINT_ROOT",
    )
    checkpoint_bucket: str = Field(
        default="",
        validation_alias="WORKER_CHECKPOINT_BUCKET",
    )
    checkpoint_cache_namespace: str = Field(
        default=DEFAULT_CHECKPOINT_CACHE_NAMESPACE,
        validation_alias="WORKER_CHECKPOINT_CACHE_NAMESPACE",
    )
    artifact_retention_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_ARTIFACT_RETENTION_ENABLED",
    )
    artifact_retention_interval_seconds: float = Field(
        default=5 * 60,
        gt=0,
        validation_alias="WORKER_ARTIFACT_RETENTION_INTERVAL_SECONDS",
    )
    image_cache_max_bytes: int = Field(
        default=DEFAULT_WORKER_IMAGE_CACHE_MAX_BYTES,
        validation_alias="WORKER_IMAGE_CACHE_MAX_BYTES",
    )
    image_materialization_max_bytes: int = Field(
        default=DEFAULT_WORKER_IMAGE_MATERIALIZATION_MAX_BYTES,
        validation_alias="WORKER_IMAGE_MATERIALIZATION_MAX_BYTES",
    )
    checkpoint_cache_max_bytes: int = Field(
        default=DEFAULT_WORKER_CHECKPOINT_CACHE_MAX_BYTES,
        validation_alias="WORKER_CHECKPOINT_CACHE_MAX_BYTES",
    )
    artifact_retention_low_watermark_pct: float = Field(
        default=0.75,
        validation_alias="WORKER_ARTIFACT_RETENTION_LOW_WATERMARK_PCT",
    )
    artifact_retention_recent_guard_seconds: int = Field(
        default=60 * 60,
        validation_alias="WORKER_ARTIFACT_RETENTION_RECENT_GUARD_SECONDS",
    )
    image_materialization_retention_seconds: int = Field(
        default=24 * 60 * 60,
        validation_alias="WORKER_IMAGE_MATERIALIZATION_RETENTION_SECONDS",
    )
    checkpoint_retention_seconds: int = Field(
        default=7 * 24 * 60 * 60,
        validation_alias="WORKER_CHECKPOINT_RETENTION_SECONDS",
    )
    data_storage_mode: StorageMountMode | None = Field(
        default=None,
        validation_alias="DATA_STORAGE_MODE",
    )
    data_storage_path: str | None = Field(
        default=None,
        validation_alias="DATA_STORAGE_PATH",
    )
    data_storage_bucket: str = Field(
        default=OBJECT_STORE_BUCKET,
        validation_alias=AliasChoices("DATA_STORAGE_BUCKET", "LAZYCLOUD_OBJECT_STORE_BUCKET"),
    )
    data_storage_endpoint_url: str = Field(
        default="http://localhost:9000",
        validation_alias=AliasChoices(
            "DATA_STORAGE_ENDPOINT_URL",
            "LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL",
        ),
    )
    data_storage_region_name: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices(
            "DATA_STORAGE_REGION_NAME",
            "LAZYCLOUD_OBJECT_STORE_REGION_NAME",
        ),
    )
    data_storage_access_key_id: str = Field(
        default=OBJECT_STORE_ACCESS_KEY_ID,
        validation_alias=AliasChoices(
            "DATA_STORAGE_ACCESS_KEY_ID",
            "LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID",
        ),
    )
    data_storage_secret_access_key: str = Field(
        default=OBJECT_STORE_SECRET_ACCESS_KEY,
        validation_alias=AliasChoices(
            "DATA_STORAGE_SECRET_ACCESS_KEY",
            "LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY",
        ),
    )
    data_storage_force_path_style: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "DATA_STORAGE_FORCE_PATH_STYLE",
            "LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE",
        ),
    )
    data_storage_juicefs_redis_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "DATA_STORAGE_JUICEFS_REDIS_URL",
            "LAZYCLOUD_JUICEFS_REDIS_URL",
        ),
    )
    data_storage_juicefs_filesystem_name: str = Field(
        default=NAME,
        validation_alias="DATA_STORAGE_JUICEFS_FILESYSTEM_NAME",
    )
    data_storage_juicefs_cache_size: int = Field(
        default=0,
        validation_alias="DATA_STORAGE_JUICEFS_CACHE_SIZE",
    )
    data_storage_juicefs_block_size: int = Field(
        default=4096,
        validation_alias="DATA_STORAGE_JUICEFS_BLOCK_SIZE",
    )
    data_storage_juicefs_prefetch: int = Field(
        default=1,
        validation_alias="DATA_STORAGE_JUICEFS_PREFETCH",
    )
    data_storage_juicefs_buffer_size: int = Field(
        default=300,
        validation_alias="DATA_STORAGE_JUICEFS_BUFFER_SIZE",
    )
    workspace_storage_mode: WorkerStorageMode = Field(
        default=WorkerStorageMode.JuiceFs,
        validation_alias="WORKER_WORKSPACE_STORAGE_MODE",
    )
    workspace_storage_base_mount_path: str = Field(
        default="/workspace",
        validation_alias="WORKER_WORKSPACE_STORAGE_BASE_MOUNT_PATH",
    )
    workspace_storage_mountpoint_binary: str = Field(
        default="ms3",
        validation_alias="WORKER_WORKSPACE_STORAGE_MOUNTPOINT_BINARY",
    )
    pool_mode: WorkerPoolMode | None = Field(
        default=None,
        validation_alias="WORKER_POOL_MODE",
    )
    metrics_enabled: bool | None = Field(
        default=None,
        validation_alias="WORKER_METRICS_ENABLED",
    )
    metrics_interval_seconds: float | None = Field(
        default=None,
        validation_alias="WORKER_METRICS_INTERVAL_SECONDS",
    )
    container_cost_hook_endpoint: str = Field(
        default="",
        validation_alias="WORKER_CONTAINER_COST_HOOK_ENDPOINT",
    )
    container_cost_hook_token: str = Field(
        default="",
        validation_alias="WORKER_CONTAINER_COST_HOOK_TOKEN",
    )
    container_cost_hook_timeout_seconds: float = Field(
        default=10.0,
        validation_alias="WORKER_CONTAINER_COST_HOOK_TIMEOUT_SECONDS",
    )
    gpu_devices: str = Field(
        default="",
        validation_alias=AliasChoices("WORKER_GPU_DEVICES", "NVIDIA_VISIBLE_DEVICES"),
    )
    nvidia_cdi_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_NVIDIA_CDI_ENABLED",
    )

    @field_validator("bundle_root", "cache_root", mode="after")
    @classmethod
    def expand_path(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve() if value is not None else None

    @field_validator("container_service_port")
    @classmethod
    def port_must_be_valid(cls, value: int) -> int:
        if value < 0 or value > 65535:
            msg = "container service port must be between 0 and 65535"
            raise ValueError(msg)
        return value

    @field_validator("capacity_owner_id")
    @classmethod
    def capacity_owner_id_must_be_canonical(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and re.fullmatch(CAPACITY_OWNER_ID_PATTERN, normalized) is None:
            raise ValueError("worker capacity owner id must be a canonical UUID")
        return normalized

    @field_validator(
        "cpu_millicores",
        "memory_mib",
        "gpu_count",
        "image_cache_max_bytes",
        "image_materialization_max_bytes",
        "checkpoint_cache_max_bytes",
        "artifact_retention_recent_guard_seconds",
        "image_materialization_retention_seconds",
        "checkpoint_retention_seconds",
    )
    @classmethod
    def capacity_values_cannot_be_negative(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value < 0:
            msg = "worker capacity values cannot be negative"
            raise ValueError(msg)
        return value

    @field_validator("artifact_retention_low_watermark_pct")
    @classmethod
    def artifact_low_watermark_must_be_valid(cls, value: float) -> float:
        if not 0 < value <= 1:
            msg = "worker artifact retention low watermark must be in (0, 1]"
            raise ValueError(msg)
        return value

    @property
    def resolved_runtime(self) -> OciRuntimeName:
        return self.runtime or self.configuration.execution.runtime

    @property
    def resolved_runtimes(self) -> list[OciRuntimeName]:
        return list(self.configuration.execution.runtimes)

    @property
    def resolved_cpu_millicores(self) -> int:
        value = self.cpu_millicores
        return value if value is not None else self.configuration.execution.capacity.cpu_millicores

    @property
    def resolved_memory_mib(self) -> int:
        value = self.memory_mib
        return value if value is not None else self.configuration.execution.capacity.memory_mib

    @property
    def resolved_gpu_type(self) -> str:
        value = self.gpu_type
        return value if value is not None else self.configuration.execution.capacity.gpu_type

    @property
    def resolved_gpu_count(self) -> int:
        value = self.gpu_count
        return value if value is not None else self.configuration.execution.capacity.gpu_count

    @property
    def resolved_requires_pool_selector(self) -> bool:
        value = self.requires_pool_selector
        if value is not None:
            return value
        return self.configuration.execution.requires_pool_selector

    @property
    def resolved_preemptible(self) -> bool:
        value = self.preemptible
        return value if value is not None else self.configuration.execution.preemptible

    @property
    def resolved_persistent(self) -> bool:
        value = self.persistent
        return value if value is not None else self.configuration.execution.persistent

    @property
    def resolved_agent_worker(self) -> bool:
        value = self.agent_worker
        return value if value is not None else self.configuration.execution.agent_worker

    @property
    def resolved_pool_mode(self) -> WorkerPoolMode:
        return self.pool_mode or self.configuration.execution.pool_mode

    @property
    def resolved_route_transport(self) -> BackendRouteTransport:
        return self.route_transport or self.configuration.network.route_transport

    @property
    def resolved_agent_bridge_network(self) -> bool:
        value = self.agent_bridge_network
        if value is not None:
            return value
        return self.configuration.network.agent_bridge_network

    @property
    def resolved_bundle_root(self) -> Path:
        return self.bundle_root or self.configuration.paths.bundle_root

    @property
    def resolved_image_cache_path(self) -> str:
        return self.image_cache_path or self.configuration.paths.image_cache_path

    @property
    def resolved_image_mount_root(self) -> str:
        return self.image_mount_root or self.configuration.paths.image_mount_root

    @property
    def resolved_source_cache_root(self) -> Path:
        if self.source_cache_root is not None:
            return self.source_cache_root.expanduser().resolve()
        configured = self.configuration.paths.source_cache_root
        if configured is not None:
            return configured.expanduser().resolve()
        cache_root = self.resolved_cache_root
        if cache_root is not None:
            return (cache_root / "source-code").expanduser().resolve()
        return DEFAULT_SOURCE_CACHE_ROOT

    @property
    def resolved_image_build_root(self) -> Path:
        return self.configuration.paths.image_build_root.expanduser().resolve()

    @property
    def resolved_cache_root(self) -> Path | None:
        if "cache_root" in self.model_fields_set:
            return self.cache_root
        return self.configuration.paths.cache_root

    @property
    def resolved_checkpoint_root(self) -> str:
        return self.checkpoint_root or self.configuration.paths.checkpoint_root

    @property
    def resolved_data_storage_mode(self) -> StorageMountMode:
        return self.data_storage_mode or self.configuration.data_storage.mode

    @property
    def resolved_data_storage_path(self) -> str:
        return self.data_storage_path or self.configuration.data_storage.path

    @property
    def resolved_metrics_enabled(self) -> bool:
        value = self.metrics_enabled
        if value is not None:
            return value
        return self.configuration.monitoring.metrics_enabled

    @property
    def resolved_metrics_interval_seconds(self) -> float:
        value = self.metrics_interval_seconds
        if value is not None:
            return value
        return self.configuration.monitoring.metrics_interval_seconds

    @property
    def resolved_network_prefix(self) -> str:
        return (
            self.network_prefix
            or self.configuration.network.network_prefix
            or self.pool_name
            or self.worker_id
        )

    @property
    def worker_repository_endpoint(self) -> str:
        return self.worker_repository_url.rstrip("/")

    @property
    def gateway_runtime_http_endpoint(self) -> str:
        return self.gateway_runtime_http_url.rstrip("/")


@dataclass(slots=True)
class BrokeredImageArchiveSourceLoader:
    repository: WorkerRepositoryHttpClient
    timeout_seconds: float = 60.0

    def load_source_image_archive(
        self,
        request: WorkerImageSourceLoadRequest,
    ) -> WorkerImageSourceLoadResult:
        credentials = self.repository.get_cache_origin_credentials(
            CacheOriginCredentialRequest(
                workspace_id=request.workspace_id,
                container_id=request.container_id,
                stub_id=request.stub_id,
                image_id=request.image_id,
            )
        ).credentials
        if credentials is None:
            return WorkerImageSourceLoadResult(
                ok=False,
                archive_path=request.archive_path,
                reason="broker did not return image archive credentials",
            )
        if not credentials.ok:
            return WorkerImageSourceLoadResult(
                ok=False,
                archive_path=request.archive_path,
                reason=credentials.error_msg or "broker denied image archive credentials",
            )
        if credentials.image_archive_url:
            return self._download_url(
                credentials.image_archive_url,
                request.archive_path,
                archive_size_bytes=credentials.archive_size_bytes,
                archive_sha256=credentials.archive_sha256,
            )
        return WorkerImageSourceLoadResult(
            ok=False,
            archive_path=request.archive_path,
            reason="broker did not return an image archive URL",
        )

    def _download_url(
        self,
        url: str,
        archive_path: str,
        *,
        archive_size_bytes: int,
        archive_sha256: str,
    ) -> WorkerImageSourceLoadResult:
        target = Path(archive_path)
        try:
            bytes_written = download_image_archive(
                url,
                target,
                archive_size_bytes=archive_size_bytes,
                archive_sha256=archive_sha256,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:
            return WorkerImageSourceLoadResult(
                ok=False,
                archive_path=archive_path,
                reason=f"brokered image archive download failed: {type(exc).__name__}: {exc}",
            )
        return WorkerImageSourceLoadResult(
            ok=True,
            archive_path=str(target),
            bytes_written=bytes_written,
            reason="brokered image archive downloaded and verified",
        )


def _normalize_image_archive_member_name(name: str) -> str | None:
    if not name or "\x00" in name:
        msg = f"unsafe image archive member name: {name!r}"
        raise ValueError(msg)
    normalized = posixpath.normpath(name)
    if normalized == ".":
        return None
    if posixpath.isabs(name) or normalized == ".." or normalized.startswith("../"):
        msg = f"unsafe image archive member path: {name!r}"
        raise ValueError(msg)
    return normalized


def _normalize_image_archive_hardlink_target(linkname: str) -> str:
    if not linkname or "\x00" in linkname:
        msg = f"unsafe image archive hard link target: {linkname!r}"
        raise ValueError(msg)
    target = linkname[1:] if posixpath.isabs(linkname) else linkname
    normalized = posixpath.normpath(target)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        msg = f"unsafe image archive hard link target: {linkname!r}"
        raise ValueError(msg)
    return normalized


def _image_root_relative_symlink_target(member_name: str, linkname: str) -> str:
    if not linkname or "\x00" in linkname:
        msg = f"unsafe image archive symbolic link target: {linkname!r}"
        raise ValueError(msg)
    member_parent = posixpath.dirname(member_name)
    member_parent_root = f"/{member_parent}" if member_parent else "/"
    if posixpath.isabs(linkname):
        target_root_path = posixpath.normpath(linkname)
    else:
        target_root_path = posixpath.normpath(posixpath.join(member_parent_root, linkname))
    if not target_root_path.startswith("/"):
        target_root_path = f"/{target_root_path}"
    return posixpath.relpath(target_root_path, member_parent_root)


def _image_archive_tar_filter(
    member: tarfile.TarInfo,
    destination: str,
) -> tarfile.TarInfo | None:
    del destination
    normalized_name = _normalize_image_archive_member_name(member.name)
    if normalized_name is None:
        return None

    updated_name = member.name
    updated_linkname = member.linkname
    if normalized_name != member.name:
        updated_name = normalized_name
    if member.issym():
        updated_linkname = _image_root_relative_symlink_target(
            normalized_name,
            member.linkname,
        )
    elif member.islnk():
        updated_linkname = _normalize_image_archive_hardlink_target(member.linkname)

    if updated_name == member.name and updated_linkname == member.linkname:
        return member
    return member.replace(name=updated_name, linkname=updated_linkname)


@dataclass(slots=True)
class TarImageArchiveMounter:
    clear_existing: bool = False

    def mount_image_archive(self, request: WorkerImageMountRequest) -> WorkerImageMountResult:
        archive_path = Path(request.archive_path)
        mount_point = Path(request.mount_point)
        mount_exists = mount_point.exists() or mount_point.is_symlink()
        if mount_exists and not self.clear_existing:
            manifest = _read_image_mount_manifest(
                mount_point,
                image_id=request.image_id,
                archive_path=archive_path,
            )
            if manifest is not None:
                mount_point.touch(exist_ok=True)
                if archive_path.is_file():
                    archive_path.touch(exist_ok=True)
                return WorkerImageMountResult(
                    status=WorkerImageMountStatus.Ready,
                    mount_point=str(mount_point),
                    reason="complete image mount already exists",
                )
            if not request.repair_incomplete:
                return WorkerImageMountResult(
                    status=WorkerImageMountStatus.RepairRequired,
                    mount_point=str(mount_point),
                    reason="image mount is incomplete and requires archive materialization",
                )
        if not archive_path.exists() or not archive_path.is_file():
            return WorkerImageMountResult(
                status=WorkerImageMountStatus.Failed,
                mount_point=str(mount_point),
                reason=f"image archive not found: {archive_path}",
            )
        if not tarfile.is_tarfile(archive_path):
            return WorkerImageMountResult(
                status=WorkerImageMountStatus.Failed,
                mount_point=str(mount_point),
                reason=f"image archive is not a tar archive: {archive_path}",
            )
        temp_mount = mount_point.with_name(f".{mount_point.name}.{uuid4().hex}.tmp")
        temp_mount.mkdir(parents=True, exist_ok=False)
        try:
            with tarfile.open(archive_path) as archive:
                members = archive.getmembers()
                if not members:
                    raise RuntimeError("image archive does not contain filesystem entries")
                archive.extractall(temp_mount, filter=_image_archive_tar_filter)
            manifest_path = temp_mount / IMAGE_MOUNT_MANIFEST_NAME
            _remove_image_mount_path(manifest_path)
            manifest_path.write_text(
                ImageMountManifest(
                    image_id=request.image_id,
                    archive_size_bytes=archive_path.stat().st_size,
                    archive_entry_count=len(members),
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            if mount_point.exists() or mount_point.is_symlink():
                if not self.clear_existing:
                    existing = _read_image_mount_manifest(
                        mount_point,
                        image_id=request.image_id,
                        archive_path=archive_path,
                    )
                    if existing is not None:
                        mount_point.touch(exist_ok=True)
                        if archive_path.is_file():
                            archive_path.touch(exist_ok=True)
                        shutil.rmtree(temp_mount, ignore_errors=True)
                        return WorkerImageMountResult(
                            status=WorkerImageMountStatus.Ready,
                            mount_point=str(mount_point),
                            reason="complete image mount already exists",
                        )
                _remove_image_mount_path(mount_point)
            mount_point.parent.mkdir(parents=True, exist_ok=True)
            try:
                temp_mount.replace(mount_point)
            except OSError:
                concurrent = _read_image_mount_manifest(
                    mount_point,
                    image_id=request.image_id,
                    archive_path=archive_path,
                )
                if concurrent is None:
                    raise
                mount_point.touch(exist_ok=True)
                archive_path.touch(exist_ok=True)
                shutil.rmtree(temp_mount, ignore_errors=True)
                return WorkerImageMountResult(
                    status=WorkerImageMountStatus.Ready,
                    mount_point=str(mount_point),
                    reason="complete image mount won concurrent materialization",
                )
        except Exception as exc:
            shutil.rmtree(temp_mount, ignore_errors=True)
            return WorkerImageMountResult(
                status=WorkerImageMountStatus.Failed,
                mount_point=str(mount_point),
                reason=f"image archive materialization failed: {type(exc).__name__}: {exc}",
            )
        return WorkerImageMountResult(
            status=WorkerImageMountStatus.Ready,
            mount_point=str(mount_point),
            reason="image archive materialized",
        )


def _read_image_mount_manifest(
    mount_point: Path,
    *,
    image_id: str,
    archive_path: Path,
) -> ImageMountManifest | None:
    if not mount_point.is_dir() or mount_point.is_symlink():
        return None
    manifest_path = mount_point / IMAGE_MOUNT_MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    try:
        manifest = ImageMountManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if manifest.image_id != image_id:
        return None
    if archive_path.is_file() and archive_path.stat().st_size != manifest.archive_size_bytes:
        return None
    return manifest


def _remove_image_mount_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)


@dataclass(slots=True)
class RemoteCheckpointPersister:
    repository: WorkerRepositoryHttpClient
    checkpoint_bucket: str
    cache_namespace: str = DEFAULT_CHECKPOINT_CACHE_NAMESPACE

    def persist_checkpoint(
        self,
        plan: CheckpointPersistencePlan,
    ) -> WorkerCheckpointPersistenceResult:
        if plan.error_message:
            raise RuntimeError(plan.error_message)
        if not self.checkpoint_bucket:
            msg = "checkpoint object bucket is required"
            raise RuntimeError(msg)
        archive_path = Path(plan.archive_path)
        checkpoint_path = Path(plan.checkpoint_path)
        if plan.remove_existing_archive:
            archive_path.unlink(missing_ok=True)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        _create_tar(checkpoint_path, archive_path, arcname=plan.checkpoint_id)
        cache_hash, size_bytes = _file_hash_and_size(archive_path)
        try:
            prepared = self.repository.prepare_checkpoint_archive_upload(
                PrepareCheckpointArchiveUploadRequest(
                    checkpoint_id=plan.checkpoint_id,
                    origin_key=plan.origin_key,
                    cache_hash=cache_hash,
                    cache_size_bytes=size_bytes,
                    checkpoint_bucket=self.checkpoint_bucket,
                )
            )
            if not prepared.upload_url:
                msg = "checkpoint archive upload URL was not returned"
                raise RuntimeError(msg)
            _put_presigned_checkpoint_archive(
                prepared.upload_url,
                archive_path,
                content_length=size_bytes,
            )
            response = self.repository.persist_checkpoint_archive(
                PersistCheckpointArchiveRequest(
                    checkpoint_id=plan.checkpoint_id,
                    origin_key=plan.origin_key,
                    cache_hash=cache_hash,
                    cache_size_bytes=size_bytes,
                    checkpoint_bucket=self.checkpoint_bucket,
                    cache_namespace=self.cache_namespace,
                    locality=plan.metadata.locality if plan.metadata is not None else "",
                    accelerator=(plan.metadata.accelerator if plan.metadata is not None else ""),
                )
            )
            return WorkerCheckpointPersistenceResult(
                checkpoint_id=response.checkpoint_id or plan.checkpoint_id,
                archive_path=str(archive_path),
                origin_key=response.origin_key or plan.origin_key,
                cache_hash=response.cache_hash or cache_hash,
                cache_size_bytes=response.cache_size_bytes or size_bytes,
                locality=response.locality,
                accelerator=response.accelerator,
            )
        finally:
            if plan.cleanup_archive_after_persist:
                archive_path.unlink(missing_ok=True)


def _put_presigned_checkpoint_archive(
    url: str,
    path: Path,
    *,
    content_length: int,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        msg = f"unsupported checkpoint upload URL scheme: {parsed.scheme}"
        raise ValueError(msg)
    connection_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(parsed.hostname or "", parsed.port, timeout=300)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    try:
        with path.open("rb") as source:
            connection.request(
                "PUT",
                target,
                body=source,
                headers={
                    "content-type": "application/x-tar",
                    "content-length": str(content_length),
                },
            )
            response = connection.getresponse()
            response.read(4096)
            if response.status < 200 or response.status >= 300:
                raise _PresignedTransferError(
                    f"checkpoint archive upload returned HTTP {response.status}"
                )
    except _PresignedTransferError:
        raise
    except Exception as exc:
        raise _PresignedTransferError(
            f"checkpoint archive upload failed: {type(exc).__name__}"
        ) from None
    finally:
        connection.close()


@dataclass(slots=True)
class RemoteCheckpointRestoreSource:
    repository: WorkerRepositoryHttpClient
    checkpoint_bucket: str
    timeout_seconds: float = 300.0
    download_urls: dict[str, str] = field(default_factory=dict)

    def get_checkpoint(self, checkpoint_id: str, *, workspace_id: str) -> CheckpointRecord:
        response = self.repository.get_checkpoint_restore(
            GetCheckpointRestoreRequest(
                checkpoint_id=checkpoint_id,
                workspace_id=workspace_id,
                checkpoint_bucket=self.checkpoint_bucket,
            )
        )
        if response.checkpoint is None or not response.download_url:
            msg = f"checkpoint {checkpoint_id!r} restore metadata was not returned"
            raise RuntimeError(msg)
        self.download_urls[checkpoint_id] = response.download_url
        return response.checkpoint

    def download_checkpoint(self, checkpoint: CheckpointRecord, target: Path) -> None:
        url = self.download_urls.pop(checkpoint.checkpoint_id, "")
        if not url:
            msg = f"checkpoint {checkpoint.checkpoint_id!r} download URL is unavailable"
            raise RuntimeError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            _download_presigned_url(
                url,
                temporary,
                timeout_seconds=self.timeout_seconds,
                resource_name="checkpoint archive",
            )
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


@dataclass(slots=True)
class OciContainerServiceInstanceRecorder:
    instances: WorkerContainerInstanceStore
    identity: WorkerRouteIdentity
    cache_available: bool = False
    workspace_storage_available: bool = False

    def record_container_instance(
        self,
        context: ContainerExecutionContext,
        *,
        spec: OciRuntimeContainerSpec,
        mount_result: ContainerMountSetupResult,
        network_result: ContainerNetworkSetupResult | None,
        port_bindings: list[PortBinding],
    ) -> None:
        root_path = _spec_root_path(spec)
        identity = self.identity
        instance = WorkerContainerServiceInstance(
            container_id=context.request.container_id,
            root_path=root_path,
            bundle_path=spec.bundle_path,
            config_path=spec.config_path,
            top_layer_path=root_path,
            workspace_path=str(Path(root_path) / "workspace"),
            cwd=context.cwd,
            runtime=context.runtime,
            env=_spec_process_env(spec),
            request_env=list(context.request.env),
            docker_enabled=context.docker_enabled,
            docker_daemon_status=(
                SandboxDockerDaemonStatus.Stopped
                if context.docker_enabled
                else SandboxDockerDaemonStatus.Disabled
            ),
            sandbox_supervisor_token_path=spec.sandbox_supervisor_token_path,
            ports=list(context.ports),
            exposed_ports=[binding.container_port for binding in port_bindings],
            address_map=_address_map(
                context.request.container_id,
                identity,
                network_result,
                port_bindings,
            ),
            mounts=[
                SandboxContainerMount(
                    source=item.mount.local_path,
                    destination=item.mount.mount_path,
                )
                for item in mount_result.mounts
                if item.included and item.mount.local_path
            ],
            workspace_id=context.request.workspace_id,
            workspace_name=context.request.workspace_name,
            app_id=context.request.app_id,
            stub_id=context.request.stub_id,
            stub_type=context.request.stub_type,
            worker_id=identity.worker_id,
            machine_id=identity.machine_id,
            pool_name=identity.pool_name,
            route_local_target_host=identity.route_local_target_host,
            route_transport=identity.route_transport,
            agent_worker=identity.agent_worker,
            image_id=context.request.image_id,
            container_ip=(
                network_result.identity.container_ip
                if network_result is not None and network_result.identity is not None
                else ""
            ),
            workspace_storage_available=(
                self.workspace_storage_available or context.request.workspace_storage_available
            ),
            cache_available=self.cache_available,
            gpu=context.request.gpu,
            gpu_count=context.request.gpu_count,
        )
        self.instances.save_container_instance(instance)


def build_production_worker_process_services(
    *,
    settings: ProductionWorkerSettings,
    image_mounter: WorkerImageArchiveMounter | None = None,
    image_source_loader: WorkerImageArchiveSourceLoader | None = None,
    mountpoint_backend: WorkerRequestMountLifecycle | None = None,
    repository_client: WorkerRepositoryHttpClient | None = None,
    runtime_configs: dict[OciRuntimeName, RuntimeBinaryConfig] | None = None,
) -> WorkerProcessServices:
    config = settings
    identity = _worker_identity(config)
    repository = repository_client or build_worker_repository_http_client(
        endpoint=config.worker_repository_endpoint,
        token=config.worker_token,
        timeout_seconds=config.worker_repository_timeout_seconds,
    )
    image_build_scratch = ImageBuildScratchManager(
        root=config.resolved_image_build_root,
        worker_id=identity.worker_id,
        max_bytes=config.configuration.image_build.scratch_max_bytes,
        per_build_max_bytes=config.configuration.image_build.per_build_max_bytes,
        minimum_free_bytes=config.configuration.image_build.minimum_free_bytes,
        stale_seconds=config.configuration.image_build.stale_seconds,
    )
    available_runtime_configs = (
        runtime_configs if runtime_configs is not None else _available_runtime_configs(config)
    )
    runtime_config = _required_runtime_config(
        config.resolved_runtime,
        available_runtime_configs,
    )
    instance_runtime = ContainerInstanceRuntimeResolver()
    runtime = OciRuntimeCommandController(
        runtime_config=runtime_config,
        runtime_configs=available_runtime_configs,
        container_runtime=instance_runtime,
    )
    checkpoint_activity = CheckpointArtifactLeaseRegistry()
    cost_resolver = _container_cost_resolver(config)
    source_materializer = SourceCodePackageMaterializer(
        cache_root=config.resolved_source_cache_root,
        cache_max_bytes=config.configuration.source_cache.max_bytes,
        cache_max_entries=config.configuration.source_cache.max_entries,
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
    credential_hydrator = WorkerCredentialHydrator(
        credentials=RemoteWorkerCredentialService(repository),
    )
    event_sink = RemoteWorkerEventSink(repository)
    usage_recorder = RemoteWorkerUsageRecorder(repository)
    log_sink = RemoteSandboxProcessLogSink(repository)
    container_log_capture = WorkerContainerLogCaptureService(RemoteContainerLogSink(repository))
    checkpoint_state_sink = RemoteCheckpointStateSink(repository)
    automatic_checkpoint_leases = RemoteAutomaticCheckpointCreationLeaseCoordinator(repository)
    checkpoint_restore_source = RemoteCheckpointRestoreSource(
        repository,
        checkpoint_bucket=config.checkpoint_bucket,
    )
    checkpoints = RuntimeCheckpointCreator(
        runtime=runtime,
        state_sink=checkpoint_state_sink,
        persister=RemoteCheckpointPersister(
            repository,
            checkpoint_bucket=config.checkpoint_bucket,
            cache_namespace=config.checkpoint_cache_namespace,
        ),
        checkpoint_root=config.resolved_checkpoint_root,
        origin_storage_available=bool(config.checkpoint_bucket),
        content_cache_available=True,
        checkpoint_activity=checkpoint_activity,
    )
    lifecycle_events = AsyncContainerLifecycleSink(
        RemoteContainerLifecycleSink(repository, worker_id=identity.worker_id)
    )
    runtime_monitor = WorkerContainerRuntimeMonitor(
        metrics=(
            WorkerContainerMetricsService(
                worker_id=identity.worker_id,
                sink=RemoteContainerMetricsSink(repository),
            )
            if config.resolved_metrics_enabled
            else None
        ),
        metrics_source_factory=(
            ProcessTreeContainerMetricsSourceFactory() if config.resolved_metrics_enabled else None
        ),
        usage_recorder=WorkerSupervisionService(
            worker_id=identity.worker_id,
            event_sink=event_sink,
            usage_recorder=usage_recorder,
            cost_resolver=cost_resolver,
            pool_mode=config.resolved_pool_mode,
        ),
        settings=ContainerRuntimeMonitorSettings(
            sample_interval_seconds=config.resolved_metrics_interval_seconds
        ),
    )
    image_build_credential_loader = RemoteImageBuildCredentialLoader(repository)
    cache_server = _worker_content_cache(config)
    archive_source_loader = image_source_loader or BrokeredImageArchiveSourceLoader(repository)
    cache_metadata = (
        CacheServerImageArchiveMetadataProvider(cache_server) if cache_server is not None else None
    )
    image_loader = WorkerImageStartupLoader(
        mounter=image_mounter or TarImageArchiveMounter(),
        cache=cache_server,
        source_loader=archive_source_loader,
        cache_metadata=cache_metadata,
        image_cache_path=config.resolved_image_cache_path,
        image_mount_root=config.resolved_image_mount_root,
        image_archive_extension=config.image_archive_extension,
        storage_mode=ImageArchiveStorageMode.Local,
        publish_source_to_cache=cache_server is not None,
    )
    gpu_assigner = _gpu_assigner(config)
    workspace_storage_mounter = WorkerWorkspaceStorageManager(
        config=WorkspaceStorageConfig(
            base_mount_path=config.workspace_storage_base_mount_path,
            default_storage_mode=config.workspace_storage_mode,
            juicefs=WorkspaceJuiceFsStorageConfig(
                redis_uri=config.data_storage_juicefs_redis_url,
                cache_size=config.data_storage_juicefs_cache_size,
                block_size=config.data_storage_juicefs_block_size,
                prefetch=config.data_storage_juicefs_prefetch,
                buffer_size=config.data_storage_juicefs_buffer_size,
                filesystem_name=config.data_storage_juicefs_filesystem_name,
            ),
            mountpoint=WorkspaceMountPointStorageConfig(
                binary=config.workspace_storage_mountpoint_binary,
            ),
        ),
        mode=config.workspace_storage_mode,
    )
    image_archiver = TarContainerImageArchiver(
        target_root=Path(config.resolved_image_cache_path),
        extension=config.image_archive_extension,
    )
    image_archive_publisher = RepositoryWorkerImageArchivePublisher(repository)
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
        spec_builder=OciRuntimeSpecBuilder(
            bundle_root=config.resolved_bundle_root,
            image_mount_root=Path(config.resolved_image_mount_root),
            runtime_configs=available_runtime_configs,
            gateway_settings=_gateway_settings(config),
            managed_runtime_artifact_root=MANAGED_RUNTIME_ARTIFACT_ROOT,
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
            checkpoint_root=config.resolved_checkpoint_root,
            checkpoint_activity=checkpoint_activity,
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
            context_loader=RepositoryImageBuildContextLoader(repository),
        ),
        image_archive_publisher=image_archive_publisher,
        image_build_credential_loader=image_build_credential_loader,
    )
    artifact_retention = WorkerArtifactRetentionService(
        instances=instance_store,
        config=WorkerArtifactRetentionConfig(
            image_cache_root=Path(config.resolved_image_cache_path),
            image_mount_root=Path(config.resolved_image_mount_root),
            checkpoint_root=Path(config.resolved_checkpoint_root),
            image_archive_extension=config.image_archive_extension,
            image_cache_max_bytes=config.image_cache_max_bytes,
            image_materialization_max_bytes=config.image_materialization_max_bytes,
            checkpoint_cache_max_bytes=config.checkpoint_cache_max_bytes,
            low_watermark_pct=config.artifact_retention_low_watermark_pct,
            recent_guard_seconds=config.artifact_retention_recent_guard_seconds,
            materialization_retention_seconds=config.image_materialization_retention_seconds,
            checkpoint_retention_seconds=config.checkpoint_retention_seconds,
            cache_pruning_enabled=config.artifact_retention_enabled,
        ),
        image_build_scratch=image_build_scratch,
        checkpoint_activity=checkpoint_activity,
    )
    return build_worker_process_services(
        identity=identity,
        dependencies=dependencies,
        workers=worker_repository,
        containers=container_repository,
        instances=instance_store,
        event_sink=event_sink,
        usage_recorder=usage_recorder,
        cost_resolver=cost_resolver,
        event_source=repository,
        pool_mode=config.resolved_pool_mode,
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
        artifact_retention=artifact_retention,
    )


def _container_cost_resolver(
    config: ProductionWorkerSettings,
) -> WorkerContainerCostResolver | None:
    if not (config.container_cost_hook_endpoint and config.container_cost_hook_token):
        return None
    return HttpWorkerContainerCostResolver(
        endpoint=config.container_cost_hook_endpoint,
        token=config.container_cost_hook_token,
        timeout_seconds=config.container_cost_hook_timeout_seconds,
    )


def _worker_content_cache(
    config: ProductionWorkerSettings,
) -> FileCacheServer | WorkerCacheHttpClient | None:
    if config.cache_endpoint:
        return WorkerCacheHttpClient(
            WorkerCacheEndpoint(url=config.cache_endpoint),
            service_token=_cache_service_token(config),
        )
    if config.resolved_cache_root is not None:
        return FileCacheServer(config.resolved_cache_root)
    return None


def _cache_service_token(config: ProductionWorkerSettings) -> str:
    if config.cache_service_token:
        return config.cache_service_token
    if config.cache_service_token_file is not None:
        token = config.cache_service_token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    msg = "cache endpoint requires a configured cache service token or token file"
    raise ValueError(msg)


def _worker_identity(config: ProductionWorkerSettings) -> WorkerRouteIdentity:
    if not config.worker_id:
        msg = "worker id is required"
        raise ValueError(msg)
    return WorkerRouteIdentity(
        worker_id=config.worker_id,
        pool_name=config.pool_name,
        machine_id=config.machine_id,
        pod_address=config.pod_address,
        container_service_port=config.container_service_port,
        persistent=config.resolved_persistent,
        route_transport=config.resolved_route_transport,
        route_local_target_host=config.route_local_target_host,
        agent_worker=config.resolved_agent_worker,
    )


def _gateway_settings(config: ProductionWorkerSettings) -> GatewayServiceSettings:
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


def _gateway_runtime_network_endpoint(config: ProductionWorkerSettings) -> str:
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


def planned_scheduler_worker_record_from_settings(
    config: ProductionWorkerSettings,
) -> SchedulerWorkerRecord:
    return _scheduler_worker_record(
        _worker_identity(config),
        config,
        [config.resolved_runtime],
    )


@dataclass(slots=True)
class CacheServerImageArchiveMetadataProvider:
    cache: FileCacheServer | WorkerCacheHttpClient

    def image_archive_metadata(self, cache_path: str) -> WorkerImageArchiveCacheMetadata:
        try:
            metadata = self.cache.content_metadata(cache_path)
        except CacheUnavailableError as exc:
            return WorkerImageArchiveCacheMetadata(error=str(exc), reachable=False)
        if metadata is None or not metadata.complete:
            return WorkerImageArchiveCacheMetadata(error="content_not_found", reachable=False)
        return WorkerImageArchiveCacheMetadata(
            content_hash=metadata.content_hash,
            size_bytes=metadata.size_bytes,
            reachable=True,
        )


def _scheduler_worker_record(
    identity: WorkerRouteIdentity,
    config: ProductionWorkerSettings,
    runtime_classes: list[OciRuntimeName],
) -> SchedulerWorkerRecord:
    cpu_millicores = config.resolved_cpu_millicores
    memory_mib = config.resolved_memory_mib
    gpu_count = config.resolved_gpu_count
    return SchedulerWorkerRecord(
        worker_id=identity.worker_id,
        pool_name=identity.pool_name,
        capacity_owner_id=_required_capacity_owner_id(config),
        machine_id=identity.machine_id,
        status=SchedulerWorkerStatus.Pending,
        gpu_type=config.resolved_gpu_type,
        runtime_class=config.resolved_runtime.value,
        runtime_classes=[runtime.value for runtime in runtime_classes],
        private_worker=config.resolved_pool_mode is WorkerPoolMode.Private,
        requires_pool_selector=(
            config.resolved_requires_pool_selector
            or config.resolved_pool_mode is WorkerPoolMode.Private
        ),
        preemptible=config.resolved_preemptible,
        free_cpu_millicores=cpu_millicores,
        free_memory_mib=memory_mib,
        free_gpu_count=gpu_count,
        total_cpu_millicores=cpu_millicores,
        total_memory_mib=memory_mib,
        total_gpu_count=gpu_count,
    )


def _required_capacity_owner_id(config: ProductionWorkerSettings) -> str:
    capacity_owner_id = config.capacity_owner_id.strip()
    if not capacity_owner_id:
        raise RuntimeError("worker capacity owner id is required")
    return capacity_owner_id


def _available_runtime_configs(
    config: ProductionWorkerSettings,
) -> dict[OciRuntimeName, RuntimeBinaryConfig]:
    available: dict[OciRuntimeName, RuntimeBinaryConfig] = {}
    availability_by_runtime: dict[OciRuntimeName, RuntimeAvailability] = {}
    for runtime in config.resolved_runtimes:
        runtime_config = RuntimeBinaryConfig(runtime=runtime)
        availability = runtime_availability(
            runtime_config,
            verify=_verify_runtime_binary,
        )
        availability_by_runtime[runtime] = availability
        if availability.available:
            available[runtime] = runtime_config
    default_availability = availability_by_runtime[config.resolved_runtime]
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
    config: ProductionWorkerSettings,
    client: WorkerRepositoryHttpClient,
) -> AgentBridgeNetworkBackend | None:
    if not config.resolved_agent_bridge_network:
        return None
    backend = AgentBridgeNetworkBackend(
        SchedulerNetworkIpAllocator(
            RemoteWorkerNetworkIpRepository(client),
            network_prefix=config.resolved_network_prefix,
        )
    )
    return backend


def _initialize_network_backend(
    backend: AgentBridgeNetworkBackend,
    gateway_public_http_url: str,
) -> None:
    backend.initialize(gateway_public_http_url)


def _gpu_assigner(config: ProductionWorkerSettings) -> WorkerGpuRuntimeAssigner:
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
        host_paths=_existing_nvidia_host_paths(),
    )


def _gpu_device_indices(value: str) -> list[int]:
    indices: list[int] = []
    for token in value.split(","):
        candidate = token.strip()
        if candidate.isdigit():
            indices.append(int(candidate))
    return sorted(dict.fromkeys(indices))


def _existing_nvidia_host_paths() -> set[str]:
    candidates = ("/usr/local/cuda-12.4", "/usr/local/nvidia/lib64")
    return {path for path in candidates if Path(path).exists()}


def _spec_root_path(spec: OciRuntimeContainerSpec) -> str:
    root = _oci_spec_document(spec).get("root")
    if isinstance(root, dict):
        path = root.get("path")
        if isinstance(path, str) and path:
            return path
    return str(Path(spec.bundle_path) / "rootfs")


def _spec_process_env(spec: OciRuntimeContainerSpec) -> list[str]:
    process = _oci_spec_document(spec).get("process")
    if not isinstance(process, dict):
        return []
    env = process.get("env")
    if not isinstance(env, list):
        return []
    return [item for item in env if isinstance(item, str)]


def _oci_spec_document(spec: OciRuntimeContainerSpec) -> dict[str, JsonValue]:
    record = _JSON_OBJECT.validate_json(spec.model_dump_json())
    document = record.get("spec")
    if not isinstance(document, dict):
        msg = "OCI runtime spec must contain a JSON object"
        raise ValueError(msg)
    return document


def _download_presigned_url(
    url: str,
    target: Path,
    *,
    timeout_seconds: float,
    resource_name: str,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        msg = f"unsupported {resource_name} URL scheme: {parsed.scheme}"
        raise ValueError(msg)
    if parsed.hostname is None:
        msg = f"{resource_name} URL hostname is required"
        raise ValueError(msg)
    connection_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(parsed.hostname, parsed.port, timeout=timeout_seconds)
    request_target = parsed.path or "/"
    if parsed.query:
        request_target = f"{request_target}?{parsed.query}"
    try:
        connection.request("GET", request_target)
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            response.read(4096)
            raise _PresignedTransferError(
                f"{resource_name} download returned HTTP {response.status}"
            )
        with target.open("wb") as output:
            while chunk := response.read(PRESIGNED_DOWNLOAD_CHUNK_SIZE_BYTES):
                output.write(chunk)
    except _PresignedTransferError:
        raise
    except Exception as exc:
        raise _PresignedTransferError(
            f"{resource_name} download failed: {type(exc).__name__}"
        ) from None
    finally:
        connection.close()


class _PresignedTransferError(RuntimeError):
    pass


def _address_map(
    container_id: str,
    route_identity: WorkerRouteIdentity,
    network_result: ContainerNetworkSetupResult | None,
    port_bindings: list[PortBinding],
) -> dict[int, str]:
    container_ip = (
        network_result.identity.container_ip
        if network_result is not None and network_result.identity is not None
        else ""
    )
    selection = select_container_network(
        container_id,
        pod_address=route_identity.pod_address,
        persistent=route_identity.persistent,
        machine_id=route_identity.machine_id,
        transport=route_identity.route_transport.value,
        container_ip=container_ip,
    )
    return container_port_address_map(selection.identity, port_bindings).addresses


def _create_tar(source_path: Path, target_path: Path, *, arcname: str) -> None:
    with tarfile.open(target_path, "w") as archive:
        archive.add(source_path, arcname=arcname)


def _file_hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
