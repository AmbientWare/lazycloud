from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.mounts import MountAuthMode, normalize_mount_prefix, validate_mount_auth

WORKER_USER_CODE_VOLUME = "/mnt/code"
WORKER_USER_ARTIFACT_VOLUME = "/artifacts"
WORKER_CONTAINER_VOLUME_PATH = "/volumes"
DEFAULT_VOLUMES_PATH = "/data/volumes"
DEFAULT_OBJECTS_PATH = "/data/objects"
DEFAULT_ARTIFACTS_PATH = "/data/artifacts"
DEFAULT_VOLUMES_PREFIX = "volumes"
DEFAULT_ARTIFACTS_PREFIX = "artifacts"
DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH = "/workspace"
CONTAINER_INNER_PORT = 8001
# Matches DEFAULT_DISK in shared.deployment_records, in bytes.
DEFAULT_CONTAINER_DISK_LIMIT_BYTES = 100 * 1024**3


class WorkerStartupKind(StringEnum):
    Function = "function"
    Endpoint = "endpoint"
    Asgi = "asgi"
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
    Unfunded = "UNFUNDED"
    """The account has no card on file and has spent what it was given.

    Its own reason rather than `Admin` or `Scheduler`, because this is the one a
    customer is owed an explanation for: nothing went wrong, nobody intervened,
    and the work stopped because there is no way to bill for more of it. Recorded
    as `User` it would look like they stopped it themselves.
    """

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


class RequestMount(ContractModel):
    local_path: str = ""
    mount_path: str
    link_path: str = ""
    read_only: bool = False
    mount_type: RequestMountType = RequestMountType.Local
    mountpoint_config: RequestMountPointConfig | None = None
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


class WorkerContainerRequestPayload(ContractModel):
    image_id: str = ""
    # Digest of the image archive this dispatch is authorized to read. The archive
    # object is global and carries no workspace component, so the control plane
    # resolving it against the requesting workspace is what makes the digest safe
    # for a worker to trust as a local cache key. Empty means no archive is
    # authorized for this image, which is the local registry-store case.
    archive_sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
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
    runtime: OciRuntimeName = OciRuntimeName.Runsc
    docker_enabled: bool = False
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)
    memory_enforced: bool = True
    memory_limit_bytes: int | None = None
    # Per-container disk ceiling for the container's writable layer. A cap, not
    # a reservation: the scheduler does not fit against it. Never optional: a
    # container without a ceiling is the unbounded case this exists to prevent.
    disk_limit_bytes: int = DEFAULT_CONTAINER_DISK_LIMIT_BYTES
    cgroup_path: str | None = None
    run_delayed_cleanup: bool = True

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
    "DEFAULT_ARTIFACTS_PATH",
    "DEFAULT_ARTIFACTS_PREFIX",
    "DEFAULT_CONTAINER_DISK_LIMIT_BYTES",
    "DEFAULT_OBJECTS_PATH",
    "DEFAULT_VOLUMES_PATH",
    "DEFAULT_VOLUMES_PREFIX",
    "DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH",
    "WORKER_CONTAINER_VOLUME_PATH",
    "WORKER_USER_ARTIFACT_VOLUME",
    "WORKER_USER_CODE_VOLUME",
    "ContainerShutdownTarget",
    "OciRuntimeName",
    "RequestMount",
    "RequestMountPointConfig",
    "RequestMountType",
    "RuntimeContainerStatus",
    "StopContainerReason",
    "WorkerContainerRequestPayload",
    "WorkerStartupKind",
]
