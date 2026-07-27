from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.mounts import MountAuthMode, normalize_mount_prefix, validate_mount_auth

WORKER_USER_CODE_VOLUME = "/mnt/code"
WORKER_USER_OUTPUT_VOLUME = "/outputs"
WORKER_CONTAINER_VOLUME_PATH = "/volumes"
DEFAULT_VOLUMES_PATH = "/data/volumes"
DEFAULT_OBJECTS_PATH = "/data/objects"
DEFAULT_OUTPUTS_PATH = "/data/outputs"
DEFAULT_VOLUMES_PREFIX = "volumes"
DEFAULT_OUTPUTS_PREFIX = "outputs"
DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH = "/workspace"
DEFAULT_DATA_STORAGE_PATH = "/data"
CONTAINER_INNER_PORT = 8001


class WorkerStartupKind(StringEnum):
    Function = "function"
    Endpoint = "endpoint"
    Asgi = "asgi"
    TaskQueue = "taskqueue"
    Pod = "pod"
    PodRun = "pod-run"
    Sandbox = "sandbox"
    Unknown = "unknown"


class OciRuntimeName(StringEnum):
    Runc = "runc"
    Runsc = "runsc"


class RuntimeContainerStatus(StringEnum):
    Creating = "creating"
    Created = "created"
    Running = "running"
    Paused = "paused"
    Stopped = "stopped"
    Unknown = "unknown"


class StopContainerReason(StringEnum):
    Ttl = "TTL"
    User = "USER"
    Scheduler = "SCHEDULER"
    Preempted = "PREEMPTED"
    Admin = "ADMIN"
    Unknown = "UNKNOWN"


class ContainerShutdownTarget(ContractModel):
    container_id: str
    worker_id: str = ""


class RequestMountType(StringEnum):
    Local = "local"
    Volume = "volume"
    MountPoint = "mountpoint"


class RequestMountPointConfig(ContractModel):
    bucket_name: str
    prefix: str = ""
    auth_mode: MountAuthMode = MountAuthMode.Ambient
    access_key: str = ""
    secret_key: str = ""
    endpoint_url: str = ""
    region: str = ""
    force_path_style: bool = False

    @field_validator("prefix")
    @classmethod
    def prefix_must_be_mountpoint_compatible(cls, value: str) -> str:
        return normalize_mount_prefix(value)

    @model_validator(mode="after")
    def credentials_match_auth_mode(self) -> RequestMountPointConfig:
        validate_mount_auth(
            self.auth_mode,
            self.access_key,
            self.secret_key,
            allow_unhydrated_secret_references=True,
        )
        return self


class RequestVolumeStoreConfig(ContractModel):
    """Identity of the platform store backing a volume mount.

    Carries no credentials. The worker resolves the store from its own
    configuration; this only names which filesystem and which path within it the
    mount must resolve to, so a worker can refuse a mount it cannot back.
    """

    filesystem_name: str
    root_path: str = DEFAULT_DATA_STORAGE_PATH
    relative_path: str

    @field_validator("filesystem_name", "relative_path")
    @classmethod
    def value_must_be_set(cls, value: str) -> str:
        if not value.strip():
            msg = "volume store filesystem name and relative path are required"
            raise ValueError(msg)
        return value

    @field_validator("relative_path")
    @classmethod
    def relative_path_must_stay_inside_store(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            msg = "volume store relative path must be relative and must not traverse upward"
            raise ValueError(msg)
        return value


class RequestMount(ContractModel):
    local_path: str = ""
    mount_path: str
    link_path: str = ""
    read_only: bool = False
    mount_type: RequestMountType = RequestMountType.Local
    mountpoint_config: RequestMountPointConfig | None = None
    volume_config: RequestVolumeStoreConfig | None = None
    source_object_id: str = ""
    source_sha256: str = ""
    source_download_url: str = ""

    @field_validator("mount_path")
    @classmethod
    def mount_path_must_be_set(cls, value: str) -> str:
        if not value:
            msg = "mount_path is required"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def platform_volume_declares_its_store(self) -> RequestMount:
        # A volume mount without a store is exactly the shape that silently fell
        # back to ephemeral local disk; make it unconstructible.
        if self.mount_type is RequestMountType.Volume and self.volume_config is None:
            msg = "platform volume mount requires volume_config"
            raise ValueError(msg)
        return self


class WorkerContainerRequestPayload(ContractModel):
    image_id: str = ""
    app_id: str = ""
    deployment_id: str = ""
    stub_type: str = ""
    workspace_name: str = ""
    entrypoint: list[str] = Field(default_factory=list)
    cwd: str = "/workspace"
    env: list[str] = Field(default_factory=list)
    secret_names: list[str] = Field(default_factory=list)
    gateway_token_required: bool = False
    workspace_storage_required: bool = False
    volume_store_required: bool = False
    mounts: list[RequestMount] = Field(default_factory=list)
    workspace_storage_available: bool = False
    workspace_storage_base_mount_path: str = DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH
    ports: list[int] = Field(default_factory=list)
    requested_ports: list[int] = Field(default_factory=list)
    checkpoint_exposed_ports: list[int] = Field(default_factory=list)
    checkpoint_id: str = ""
    checkpoint_enabled: bool = False
    checkpoint_readiness_path: str = ""
    checkpoint_readiness_port: int = Field(default=0, ge=0, le=65535)
    checkpoint_readiness_timeout_seconds: int = Field(default=600, ge=1)
    checkpoint_readiness_interval_seconds: float = Field(default=1.0, gt=0)
    startup_kind: WorkerStartupKind = WorkerStartupKind.Unknown
    runtime: OciRuntimeName = OciRuntimeName.Runc
    docker_enabled: bool = False
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)
    memory_enforced: bool = True
    memory_limit_bytes: int | None = None
    cgroup_path: str | None = None
    run_delayed_cleanup: bool = True
    cost_per_ms: float = 0.0

    @field_validator("ports", "requested_ports", "checkpoint_exposed_ports")
    @classmethod
    def ports_must_be_valid(cls, value: list[int]) -> list[int]:
        for port in value:
            if not 1 <= port <= 65535:
                msg = "ports must be between 1 and 65535"
                raise ValueError(msg)
        return value


__all__ = [
    "CONTAINER_INNER_PORT",
    "DEFAULT_DATA_STORAGE_PATH",
    "DEFAULT_OBJECTS_PATH",
    "DEFAULT_OUTPUTS_PATH",
    "DEFAULT_OUTPUTS_PREFIX",
    "DEFAULT_VOLUMES_PATH",
    "DEFAULT_VOLUMES_PREFIX",
    "DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH",
    "WORKER_CONTAINER_VOLUME_PATH",
    "WORKER_USER_CODE_VOLUME",
    "WORKER_USER_OUTPUT_VOLUME",
    "ContainerShutdownTarget",
    "OciRuntimeName",
    "RequestMount",
    "RequestMountPointConfig",
    "RequestMountType",
    "RequestVolumeStoreConfig",
    "RuntimeContainerStatus",
    "StopContainerReason",
    "WorkerContainerRequestPayload",
    "WorkerStartupKind",
]
