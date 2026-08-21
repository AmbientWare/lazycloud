from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

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
# Served by the runner rather than by user code, so a probe against it answers
# whether the serving loop is up even when the handler is wedged.
CONTAINER_HEALTH_PATH = "/health"
# Matches DEFAULT_DISK in shared.deployment_records, in bytes.
DEFAULT_CONTAINER_DISK_LIMIT_BYTES = 100 * 1024**3

NODE_OVERHEAD_FACTOR = 1.10
"""How much larger than the request a node has to be before it can host it.

The agent, the container runtime and the host's own daemons take their share
before a container gets anything, so a request that exactly equals a node's
advertised size leaves nothing for the processes that start the container.

Applied to the request rather than deducted from the offer because it is a fact
about every node this platform launches, not about any one workload. Both places
that decide whether capacity fits a shape read it, so a pool judged able to host
a request is sized the way a new pool would have been.
"""


def capacity_with_overhead(value: int) -> int:
    """A resource floor raised by what the node spends on itself."""
    if value <= 0:
        return value
    return math.ceil(value * NODE_OVERHEAD_FACTOR)


def schedulable_capacity(total: int) -> int:
    """What a node can give containers, after what the platform takes.

    The inverse of `capacity_with_overhead`, and the reason both exist: selection
    buys a node at least this much larger than the request, and the node then has
    to advertise less than it physically holds or placement fills back in the
    headroom selection just paid for.
    """
    if total <= 0:
        return total
    return int(total / NODE_OVERHEAD_FACTOR)


# How far past its request a container may expand when its author named no limit.
#
# Proportional rather than a flat addend. A fixed number of gibibytes above the
# request is enormous for the small workloads that ask for a few hundred mebibytes
# and irrelevant to the large ones, which is the opposite of how much is known
# about either. The floor keeps a default 128 MiB request from being held to half
# a gibibyte, where a Python interpreter and one large import already do not fit.
CONTAINER_MEMORY_BURST_FACTOR = 4.0
CONTAINER_MEMORY_BURST_FLOOR_MIB = 1024
# And bounded above, because a proportional ceiling on a large request outgrows
# the machine. Selection only guarantees a node slightly larger than the request,
# so a limit several times it is one the container can never reach: the host runs
# out first and its OOM killer picks a victim, which is the outcome a
# per-container ceiling exists to avoid.
CONTAINER_MEMORY_BURST_CAP_MIB = 8192

# Modal's soft CPU limit, deliberately: a request plus sixteen physical cores.
# Processor time is compressible, so a container over its share is throttled and
# everything on the node degrades together. Memory is not, which is why the two
# ceilings are not written the same way.
CONTAINER_CPU_BURST_CEILING_MILLICORES = 16_000


DEFAULT_MEMORY_PRESSURE_EVICTION_PERCENT = 1.0
"""Full-stall percentage over ten seconds at which a worker stops coping.

Measured rather than chosen, because the figure this replaced was chosen and
never fired. A slot pinned at its memory limit with more than its own size
swapped out -- a genuinely thrashing worker -- reads between 0.8 and 1.4 here,
and an idle one reads 0.0 to 0.2. Twenty, the first guess, describes a machine
already dead.

`full` rather than `some`: `some` counts any window where one task waited, which
a busy worker does constantly. `full` counts windows where nothing could run.
The two tracked each other closely when measured, but only because there were
two containers; `some` climbs with the container count whether or not anything
is wrong.

The margin over idle is about five times, which is thinner than it looks: a
container inside its reservation is never a candidate, and the cooldown means a
transient spike costs at most one eviction. Worth re-measuring on a node running
zram, where reclaim is faster and the stall for the same thrash will be lower.
"""


@dataclass(frozen=True, slots=True)
class ContainerMemoryReading:
    """One container's memory, as the machine currently sees it."""

    container_id: str
    current_bytes: int
    reserved_bytes: int

    @property
    def bytes_above_reservation(self) -> int:
        return self.current_bytes - self.reserved_bytes


def select_memory_eviction_candidate(
    readings: Sequence[ContainerMemoryReading],
    *,
    pressure_percent: float,
    threshold_percent: float = DEFAULT_MEMORY_PRESSURE_EVICTION_PERCENT,
) -> ContainerMemoryReading | None:
    """Which container to stop when the machine is running out of memory.

    The one furthest above what it reserved, which is the rule a reservation
    exists to make true: a container inside its request is never a candidate,
    however large it is. That is the whole contract, and it is why this decision
    cannot be left to the kernel -- `oom_badness` scores resident size and page
    tables and has no notion of what anyone was promised, so it reaches the
    biggest honest tenant before a small one that tripled.

    Returns None when nothing is over its reservation, because then there is no
    container whose growth caused this and stopping one would be arbitrary.
    """
    if pressure_percent < threshold_percent:
        return None
    over = [reading for reading in readings if reading.bytes_above_reservation > 0]
    if not over:
        return None
    # Ties broken by container id so two workers reading the same machine cannot
    # choose differently.
    return max(over, key=lambda reading: (reading.bytes_above_reservation, reading.container_id))


def container_memory_limit_mib(request_mib: int) -> int:
    """The ceiling a container is killed at when its author named none.

    The request itself stays the reservation, so a container is protected under
    node memory pressure up to what it asked for. This is only how far above that
    it may go before the kernel stops it.
    """
    if request_mib <= 0:
        return 0
    proportional = max(
        int(request_mib * CONTAINER_MEMORY_BURST_FACTOR),
        request_mib + CONTAINER_MEMORY_BURST_FLOOR_MIB,
    )
    return min(proportional, request_mib + CONTAINER_MEMORY_BURST_CAP_MIB)


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

    MemoryEvicted = "MEMORY_EVICTED"
    """The machine ran short of memory and this container was using the most
    above what it reserved.

    Its own reason rather than `Preempted`, which says a machine was reclaimed.
    Nothing was reclaimed here and the container did nothing wrong except grow
    into headroom that stopped being spare. It is also the one reason a customer
    can act on directly: raising the request moves them out of the candidate set,
    which is exactly what a reservation is for.
    """

    Unknown = "UNKNOWN"

    def describe(self) -> str:
        """Why the container stopped, for the person whose container it was.

        The fleet is not the audience. Someone reading this wants to know
        whether their work was interrupted by them, by us, or by their bill,
        and each answer sends them somewhere different.

        Short, because it is read in a table cell as often as in a sentence.

        `Unknown` describes nothing on purpose. It is the column default, so it
        also means the reason has not arrived yet, and a container that is
        simply still running would otherwise be given a cause.
        """

        return _STOP_REASON_DESCRIPTIONS[self]


_STOP_REASON_DESCRIPTIONS: dict[StopContainerReason, str] = {
    StopContainerReason.Ttl: "it reached its time limit",
    StopContainerReason.User: "it was stopped from this account",
    StopContainerReason.Scheduler: "the platform moved the work",
    StopContainerReason.Preempted: "its machine was reclaimed",
    # Not "an operator stopped it". A worker taking SIGTERM for an ordinary
    # redeploy stops everything it holds under this reason, and naming a person
    # for routine churn tells the customer something untrue.
    StopContainerReason.Admin: "the platform stopped it",
    StopContainerReason.Unfunded: "the account has no payment method on file",
    StopContainerReason.MemoryEvicted: (
        "the machine ran out of memory and this container was using the most above its request"
    ),
    StopContainerReason.Unknown: "",
}

_missing_descriptions = sorted(set(StopContainerReason) - set(_STOP_REASON_DESCRIPTIONS))
if _missing_descriptions:
    raise RuntimeError(f"stop reasons without a description: {_missing_descriptions}")


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
    """Where this container is killed, or None to take the platform default.

    Read as the effective ceiling by everything downstream: the cgroup the
    container runs under and the watcher that reports an OOM both take this
    number, so a container cannot be reported killed at a figure it was
    allowed to exceed.
    """

    cpu_limit_millicores: int = 0
    """Where this container is throttled, or zero to take the platform default."""

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
    "CONTAINER_CPU_BURST_CEILING_MILLICORES",
    "CONTAINER_HEALTH_PATH",
    "CONTAINER_INNER_PORT",
    "CONTAINER_MEMORY_BURST_CAP_MIB",
    "CONTAINER_MEMORY_BURST_FACTOR",
    "CONTAINER_MEMORY_BURST_FLOOR_MIB",
    "DEFAULT_ARTIFACTS_PATH",
    "DEFAULT_ARTIFACTS_PREFIX",
    "DEFAULT_CONTAINER_DISK_LIMIT_BYTES",
    "DEFAULT_MEMORY_PRESSURE_EVICTION_PERCENT",
    "DEFAULT_OBJECTS_PATH",
    "DEFAULT_VOLUMES_PATH",
    "DEFAULT_VOLUMES_PREFIX",
    "DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH",
    "NODE_OVERHEAD_FACTOR",
    "WORKER_CONTAINER_VOLUME_PATH",
    "WORKER_USER_ARTIFACT_VOLUME",
    "WORKER_USER_CODE_VOLUME",
    "ContainerMemoryReading",
    "ContainerShutdownTarget",
    "OciRuntimeName",
    "RequestMount",
    "RequestMountPointConfig",
    "RequestMountType",
    "RuntimeContainerStatus",
    "StopContainerReason",
    "WorkerContainerRequestPayload",
    "WorkerStartupKind",
    "capacity_with_overhead",
    "container_memory_limit_mib",
    "schedulable_capacity",
    "select_memory_eviction_candidate",
]
