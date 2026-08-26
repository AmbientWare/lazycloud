from __future__ import annotations

import hashlib
import ipaddress
import math
import posixpath
import shlex
from collections.abc import Mapping, Sequence
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import Field, JsonValue, field_validator, model_validator
from shared.container_requests import (
    CONTAINER_CPU_BURST_CEILING_MILLICORES,
    CONTAINER_INNER_PORT,
    container_memory_limit_mib,
    schedulable_capacity,
)
from shared.contracts import ContractModel
from shared.env import (
    CONTAINER_HOSTNAME_ENV,
    CONTAINER_ID_ENV,
    GATEWAY_GRPC_HOST_ENV,
    GATEWAY_GRPC_PORT_ENV,
    GATEWAY_HTTP_HOST_ENV,
    GATEWAY_HTTP_PORT_ENV,
    GATEWAY_HTTP_URL_ENV,
    STORAGE_AVAILABLE_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
)
from shared.routing import BackendRouteTransport

from worker.runtime_config import (
    DEFAULT_GVISOR_OOM_THRESHOLD_PERCENT,
    OciRuntimeName,
    OomWatcherKind,
)

CGROUP_V2_OOM_GROUP_PARAMETER = "memory.oom.group"
CGROUP_V2_MEMORY_HIGH_PARAMETER = "memory.high"
MIB = 1024 * 1024
DEFAULT_CPU_SHARE_UNIT = 1024
DEFAULT_CPU_PERIOD_US = 100_000
DEFAULT_CUDA_VERSION = "12.4"
NVIDIA_DRIVER_LIBRARY_DIR = "/usr/local/nvidia/lib64"
NVIDIA_DRIVER_BINARY_DIR = "/usr/local/nvidia/bin"
DEFAULT_CONTAINER_PATHS = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
    NVIDIA_DRIVER_BINARY_DIR,
)
DEFAULT_CONTAINER_LIBRARY_PATHS = (
    # First of the three: these are the only files here, they are matched to the
    # kernel module on the node, and a CUDA image that ships a build-time
    # `libcuda` stub of its own must not be the one a workload links against.
    NVIDIA_DRIVER_LIBRARY_DIR,
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/worker/x86_64-linux-gnu",
)
NVIDIA_DRIVER_CAPABILITIES = "compute,utility,graphics,ngx,video"
# The driver's userspace, by the file names libnvidia-container injects. A CUDA
# image links against the driver and never ships it, because the driver has to
# match the kernel module on the node, so these files are the whole difference
# between a container that can open its GPUs and one that cannot.
NVIDIA_DRIVER_LIBRARY_PREFIXES = (
    "libcuda.so",
    "libcudadebugger.so",
    "libEGL_nvidia.so",
    "libGLESv1_CM_nvidia.so",
    "libGLESv2_nvidia.so",
    "libGLX_nvidia.so",
    "libnvcuvid.so",
    "libnvidia-",
    "libnvoptix.so",
)
# What `utility` in NVIDIA_DRIVER_CAPABILITIES promises a container it will find.
NVIDIA_DRIVER_BINARY_NAMES = (
    "nvidia-cuda-mps-control",
    "nvidia-cuda-mps-server",
    "nvidia-debugdump",
    "nvidia-smi",
)
# Present on every driver install and needed by every GPU container regardless of
# which cards it was assigned: the control node, the unified-memory nodes, and the
# modeset node. Per-GPU nodes are added for the assigned indices.
NVIDIA_CONTROL_DEVICE_PATHS = (
    "/dev/nvidiactl",
    "/dev/nvidia-uvm",
    "/dev/nvidia-uvm-tools",
    "/dev/nvidia-modeset",
)
NETWORK_INTERFACE_NAME_MAX_LENGTH = 15
DEFAULT_CONTAINER_BRIDGE_NAME = "rt_br0"
DEFAULT_CONTAINER_SUBNET = "192.168.0.0/20"
DEFAULT_CONTAINER_IPV6_SUBNET = "fd00:abcd::/64"
TAILNET_SUBNET = "100.64.0.0/10"
TAILNET_IPV6_SUBNET = "fd7a:115c:a1e0::/48"
"""The tailnet's address ranges, which container traffic must be able to reach.

A workload dials the gateway at the runtime origin, and on a managed node that
origin is a tailnet peer. The tailnet leaves through its own interface rather
than the default route, so rules written against the default route alone let a
container reach the internet and nothing internal. Matched on destination rather
than on an interface name, because the range is what the reachability
requirement is about — the interface carrying it is an implementation detail.
"""
CHECKPOINT_ARCHIVE_EXTENSION = ".tar"
CHECKPOINT_ORIGIN_PREFIX = "checkpoints"
CHECKPOINT_FILESYSTEM_DIR = "filesystem"
PLATFORM_GATEWAY_ENV_KEYS = {
    GATEWAY_GRPC_HOST_ENV,
    GATEWAY_GRPC_PORT_ENV,
    GATEWAY_HTTP_HOST_ENV,
    GATEWAY_HTTP_PORT_ENV,
    GATEWAY_HTTP_URL_ENV,
}


class GatewayProtocol(StrEnum):
    Grpc = "grpc"
    Http = "http"


class NetworkAddressMode(StrEnum):
    LocalPod = "local-pod"
    AgentBridge = "agent-bridge"


class ContainerNetworkSelectionReason(StrEnum):
    LocalDefault = "local-default"
    AgentBridgePersistentMachine = "agent-bridge-persistent-machine"
    DirectTransport = "direct-transport"


class ContainerRuntimeOperation(StrEnum):
    Kill = "kill"
    Exec = "exec"
    WorkspaceSync = "workspace-sync"
    SandboxExec = "sandbox-exec"


class WorkspaceSyncOperation(StrEnum):
    Delete = "delete"
    Write = "write"
    Move = "move"


class OciMountType(StrEnum):
    Bind = "bind"
    NoneMount = "none"


class GatewayEndpointSettings(ContractModel):
    host: str = ""
    port: int = 0
    external_host: str = ""
    external_port: int = 0
    tls: bool = False

    @field_validator("port", "external_port")
    @classmethod
    def port_must_be_non_negative(cls, value: int) -> int:
        if value < 0:
            msg = "gateway ports cannot be negative"
            raise ValueError(msg)
        return value


class GatewayServiceSettings(ContractModel):
    host: str = ""
    grpc: GatewayEndpointSettings = Field(default_factory=GatewayEndpointSettings)
    http: GatewayEndpointSettings = Field(default_factory=GatewayEndpointSettings)


class GatewayContainerEnvironment(ContractModel):
    grpc_host: str
    grpc_port: str
    http_host: str
    http_port: str


class ContainerEnvironmentRequest(ContractModel):
    container_id: str
    pod_address: str
    workspace_id: str = ""
    workspace_name: str = ""
    bind_ports: list[int] = Field(default_factory=lambda: [CONTAINER_INNER_PORT])
    storage_available: bool = False
    request_env: list[str] = Field(default_factory=list)
    initial_spec_env: list[str] = Field(default_factory=list)

    @field_validator("bind_ports")
    @classmethod
    def bind_ports_must_be_valid(cls, value: list[int]) -> list[int]:
        if not value:
            msg = "at least one bind port is required"
            raise ValueError(msg)
        for port in value:
            if not 1 <= port <= 65535:
                msg = "bind ports must be between 1 and 65535"
                raise ValueError(msg)
        return value


class ContainerEnvironmentPlan(ContractModel):
    request: ContainerEnvironmentRequest
    gateway: GatewayContainerEnvironment
    hostname: str
    env: list[str]

    @property
    def env_map(self) -> dict[str, str]:
        return env_list_to_map(self.env)


class OciLinuxCpu(ContractModel):
    shares: int
    quota: int
    period: int


class OciLinuxMemory(ContractModel):
    reservation_bytes: int
    limit_bytes: int
    swap_bytes: int


class OciLinuxResources(ContractModel):
    cpu: OciLinuxCpu
    memory: OciLinuxMemory | None = None
    deferred: dict[str, str] = Field(default_factory=dict)
    """cgroup v2 settings the runtime will not install, for the worker to write.

    Kept off the OCI spec deliberately. runsc ignores `linux.resources.unified`,
    so putting them there produces a spec that asks for settings and a cgroup
    that does not have them -- which reads as working and is not.
    """

    def as_oci_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "cpu": {
                "shares": self.cpu.shares,
                "quota": self.cpu.quota,
                "period": self.cpu.period,
            }
        }
        if self.memory is not None:
            payload["memory"] = {
                "reservation": self.memory.reservation_bytes,
                "limit": self.memory.limit_bytes,
                "swap": self.memory.swap_bytes,
            }

        return payload


class ContainerResourceRequest(ContractModel):
    cpu_millicores: int
    memory_mib: int
    memory_enforced: bool = True
    cgroup_v2_oom_group: bool = True
    cpu_share_unit: int = DEFAULT_CPU_SHARE_UNIT
    cpu_period_us: int = DEFAULT_CPU_PERIOD_US
    node_cpu_millicores: int = 0
    """What the machine has, or zero when the worker could not read it."""

    node_memory_mib: int = 0
    """What the machine holds, or zero when the worker could not read it.

    Only the hard ceiling uses it, to avoid handing a container a limit larger
    than the node it runs on.
    """

    cpu_limit_millicores: int = 0
    """Throttle point this container's author chose, or zero to take the default.

    Zero is absence rather than a limit of nothing: a container throttled at zero
    would never run, so there is no value it could be confused with.
    """

    memory_limit_mib: int = 0
    """Kill point this container's author chose, or zero to take the default."""

    @field_validator("cpu_millicores", "memory_mib", "cpu_share_unit", "cpu_period_us")
    @classmethod
    def resource_numbers_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "container resource values must be positive"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def a_limit_cannot_sit_below_its_request(self) -> ContainerResourceRequest:
        # The request is the reservation. A ceiling under it would be a container
        # guaranteed more than it is allowed to use, and the kernel would kill it
        # for reaching what the scheduler promised.
        if 0 < self.cpu_limit_millicores < self.cpu_millicores:
            raise ValueError("cpu limit cannot be below the cpu request")
        if 0 < self.memory_limit_mib < self.memory_mib:
            raise ValueError("memory limit cannot be below the memory request")
        return self


class PortBinding(ContractModel):
    host_port: int
    container_port: int

    @field_validator("host_port", "container_port")
    @classmethod
    def ports_must_be_valid(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            msg = "ports must be between 1 and 65535"
            raise ValueError(msg)
        return value


class ContainerNetworkIdentity(ContractModel):
    container_id: str
    pod_address: str = ""
    container_ip: str = ""
    mode: NetworkAddressMode = NetworkAddressMode.LocalPod


class ContainerNetworkSelection(ContractModel):
    identity: ContainerNetworkIdentity
    persistent: bool = False
    machine_id: str = ""
    transport: str = ""
    reason: ContainerNetworkSelectionReason = ContainerNetworkSelectionReason.LocalDefault


class ContainerNetworkAddressMap(ContractModel):
    identity: ContainerNetworkIdentity
    addresses: dict[int, str]


class DeviceNode(ContractModel):
    """A character device as it exists on the host, read rather than assumed."""

    path: str
    major: int
    minor: int
    file_mode: int


class OciDevice(ContractModel):
    path: str
    device_type: str = "c"
    major: int
    minor: int
    file_mode: int
    uid: int = 0
    gid: int = 0

    def as_oci_dict(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "type": self.device_type,
            "major": self.major,
            "minor": self.minor,
            "fileMode": self.file_mode,
            "uid": self.uid,
            "gid": self.gid,
        }

    def as_cgroup_allow(self) -> dict[str, JsonValue]:
        """The cgroup rule that lets the container actually open the node.

        Adding the device without this leaves it visible and unopenable, which
        reads exactly like a driver fault from inside the workload.
        """
        return {
            "allow": True,
            "type": self.device_type,
            "major": self.major,
            "minor": self.minor,
            "access": "rw",
        }


class OciMount(ContractModel):
    mount_type: OciMountType = OciMountType.Bind
    source: str
    destination: str
    options: list[str] = Field(default_factory=list)

    def as_oci_dict(self) -> dict[str, JsonValue]:
        return {
            "type": self.mount_type.value,
            "source": self.source,
            "destination": self.destination,
            "options": list(self.options),
        }


class NvidiaEnvironmentPlan(ContractModel):
    env: list[str]
    cuda_version: str
    added_defaults: dict[str, str] = Field(default_factory=dict)

    @property
    def env_map(self) -> dict[str, str]:
        return env_list_to_map(self.env)


class RuntimeServerOperationPlan(ContractModel):
    operation: ContainerRuntimeOperation
    container_id: str
    ok: bool = True
    error_message: str = ""
    argv: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    cwd: str | None = None
    signal: int | None = None
    force_delete: bool = False
    requires_running: bool = False
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class WorkspaceSyncPlan(ContractModel):
    container_id: str
    operation: WorkspaceSyncOperation
    workspace_root: str
    target_path: str
    new_path: str | None = None
    is_dir: bool = False
    data_size_bytes: int = 0


class CheckpointCacheMetadata(ContractModel):
    cache_hash: str
    size_bytes: int
    origin_key: str
    locality: str = ""
    accelerator: str = "CPU"

    @field_validator("size_bytes")
    @classmethod
    def size_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "checkpoint cache size must be positive"
            raise ValueError(msg)
        return value


class WorkerOomWatcherPlan(ContractModel):
    runtime: OciRuntimeName
    enabled: bool
    watcher: OomWatcherKind | None = None
    memory_limit_bytes: int | None = None
    cgroup_path: str | None = None
    stop_container_on_oom: bool = False
    reason: str


def gateway_host_value(*values: str | None) -> str:
    for value in values:
        if value is None:
            continue
        candidate = value.strip()
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate.strip("[]"))
            return candidate.strip("[]")
        except ValueError:
            pass
        parsed = urlparse(candidate)
        if parsed.hostname:
            return parsed.hostname.strip("[]")
        if candidate.count(":") == 1:
            host, _, port = candidate.partition(":")
            if host and port.isdigit():
                return host.strip("[]")
        return candidate.strip("[]")
    return ""


def gateway_port_value(
    env_value: str | None,
    configured_external_port: int,
    configured_port: int,
    *,
    tls: bool,
) -> str:
    if env_value is not None and env_value.strip():
        return env_value.strip()
    for port in (configured_external_port, configured_port):
        if port > 0:
            return str(port)
    return "443" if tls else "80"


def resolve_gateway_environment(
    settings: GatewayServiceSettings,
    *,
    env: dict[str, str] | None = None,
) -> GatewayContainerEnvironment:
    env = env or {}
    return GatewayContainerEnvironment(
        grpc_host=gateway_host_value(
            env.get(GATEWAY_GRPC_HOST_ENV),
            settings.grpc.external_host,
            settings.grpc.host,
            settings.host,
        ),
        grpc_port=gateway_port_value(
            env.get(GATEWAY_GRPC_PORT_ENV),
            settings.grpc.external_port,
            settings.grpc.port,
            tls=settings.grpc.tls,
        ),
        http_host=gateway_host_value(
            env.get(GATEWAY_HTTP_HOST_ENV),
            settings.http.external_host,
            settings.http.host,
            settings.host,
        ),
        http_port=gateway_port_value(
            env.get(GATEWAY_HTTP_PORT_ENV),
            settings.http.external_port,
            settings.http.port,
            tls=settings.http.tls,
        ),
    )


def build_container_environment(
    request: ContainerEnvironmentRequest,
    settings: GatewayServiceSettings,
    *,
    env: dict[str, str] | None = None,
) -> ContainerEnvironmentPlan:
    gateway = resolve_gateway_environment(settings, env=env)
    request_env = env_list_to_map(request.request_env)
    gateway_http_url = (
        _gateway_http_url(gateway, tls=settings.http.tls)
        if gateway.http_host
        else request_env.get(GATEWAY_HTTP_URL_ENV, "")
    )
    bind_port = request.bind_ports[0]
    hostname = f"{request.pod_address}:{bind_port}"
    container_env = [
        f"BIND_PORT={CONTAINER_INNER_PORT}",
        f"{CONTAINER_ID_ENV}={request.container_id}",
        f"{CONTAINER_HOSTNAME_ENV}={hostname}",
        f"{WORKSPACE_ID_ENV}={request.workspace_id}",
        f"{WORKSPACE_NAME_ENV}={request.workspace_name}",
        f"{GATEWAY_GRPC_HOST_ENV}={gateway.grpc_host}",
        f"{GATEWAY_GRPC_PORT_ENV}={gateway.grpc_port}",
        f"{GATEWAY_HTTP_HOST_ENV}={gateway.http_host}",
        f"{GATEWAY_HTTP_PORT_ENV}={gateway.http_port}",
        f"{GATEWAY_HTTP_URL_ENV}={gateway_http_url}",
        f"{STORAGE_AVAILABLE_ENV}={str(request.storage_available).lower()}",
        "PYTHONUNBUFFERED=1",
    ]
    return ContainerEnvironmentPlan(
        request=request,
        gateway=gateway,
        hostname=hostname,
        env=[
            *_without_platform_gateway_env(request.request_env),
            *_without_platform_gateway_env(request.initial_spec_env),
            *container_env,
        ],
    )


def _gateway_http_url(gateway: GatewayContainerEnvironment, *, tls: bool) -> str:
    scheme = "https" if tls else "http"
    host = gateway.http_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{gateway.http_port}"


def _without_platform_gateway_env(values: list[str]) -> list[str]:
    return [
        value
        for value in values
        if value.partition("=")[0].strip() not in PLATFORM_GATEWAY_ENV_KEYS
    ]


def env_list_to_map(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, sep, raw = value.partition("=")
        if sep:
            result[key] = raw
    return result


def map_to_env_list(values: dict[str, str]) -> list[str]:
    return [f"{key}={value}" for key, value in values.items()]


def plan_oci_linux_resources(request: ContainerResourceRequest) -> OciLinuxResources:
    # shares are proportional to the request, so under contention a container
    # still gets at least what it asked for. quota is the burst ceiling, not the
    # request: capping it at the request would pin a default function to an
    # eighth of a core even on a completely idle worker.
    #
    # Bounded by the machine when the worker knows it. A flat allowance is either
    # unreachable or the whole node depending on what it landed on, and neither
    # is what "sixteen cores above the request" was meant to say.
    ceiling_millicores = request.cpu_limit_millicores or _cpu_burst_ceiling_millicores(
        request.cpu_millicores,
        node_cpu_millicores=request.node_cpu_millicores,
    )
    cpu = OciLinuxCpu(
        shares=request.cpu_millicores * request.cpu_share_unit // 1000,
        quota=ceiling_millicores * request.cpu_period_us // 1000,
        period=request.cpu_period_us,
    )
    # Not put in the OCI spec's `unified` block, which runsc ignores outright: a
    # spec asking for these produces a cgroup holding `max` and `0`. Returned for
    # the caller to write into the cgroup once the container has started.
    deferred = (
        {CGROUP_V2_OOM_GROUP_PARAMETER: "1"}
        if request.memory_enforced and request.cgroup_v2_oom_group
        else {}
    )
    memory: OciLinuxMemory | None = None
    if request.memory_enforced:
        # Four values, and they do different jobs.
        #
        # `memory.low` is the request, and it is the whole of what a reservation
        # buys: reclaim takes from containers above their request before it
        # touches one inside it. Measured on a leaking neighbour, a protected
        # container saw an 11ms worst wakeup and no major faults where an
        # unprotected one saw 941ms and 5,630.
        #
        # `memory.high` is the burst ceiling, and it throttles rather than kills.
        # `memory.max` is the wall behind it, clamped to what the machine can
        # actually honour: a ceiling larger than the node is one the container
        # never reaches, so the host runs out first and its OOM killer picks a
        # victim by size rather than by who exceeded anything.
        #
        # Swap is what makes the first two work at all. A cgroup of anonymous
        # pages with nowhere to reclaim to does not slow down at `memory.high`,
        # it stalls -- 21 seconds for an 8MiB allocation, with the kernel
        # scanning zero pages.
        reservation = request.memory_mib * MIB
        requested_high = request.memory_limit_mib or container_memory_limit_mib(request.memory_mib)
        hard_mib = _hard_memory_ceiling_mib(
            requested_high,
            request_mib=request.memory_mib,
            node_memory_mib=request.node_memory_mib,
        )
        # The throttle takes the same bound the wall does. Above it the throttle
        # is unreachable, so the container is killed at the wall having never
        # been slowed down -- the opposite of what these two values are for.
        high_mib = min(requested_high, hard_mib)
        # And it has to sit below both walls, not on them. Reclaim beginning where
        # the kernel would kill leaves no room to reclaim in; and the sandbox
        # OOM watcher trips at a percentage of the same ceiling, so a throttle
        # above that point is one the watcher reaches first -- the container
        # killed having never been slowed, which is what these two values exist
        # to avoid.
        watcher_trips_at = int(hard_mib * DEFAULT_GVISOR_OOM_THRESHOLD_PERCENT / 100)
        high_mib = min(high_mib, watcher_trips_at - 1)
        if hard_mib > request.memory_mib:
            high_mib = min(high_mib, hard_mib - max((hard_mib - request.memory_mib) // 10, 1))
        high_mib = max(high_mib, request.memory_mib)
        limit = hard_mib * MIB
        # The ordering alone is not the invariant worth checking -- the floors
        # above make `low <= high <= max` true by construction, so asserting it
        # reads as protection while proving nothing. What can still fail is the
        # relationship those floors were introduced to preserve: the point the
        # watcher kills at has to stay above what the tenant reserved.
        if watcher_trips_at < request.memory_mib:
            raise ValueError(
                "a container may not be killed inside its reservation: "
                f"low={request.memory_mib} high={high_mib} max={hard_mib} "
                f"watcher kills at {watcher_trips_at} MiB"
            )
        memory = OciLinuxMemory(
            reservation_bytes=reservation,
            limit_bytes=limit,
            # OCI states memory-plus-swap, so a value equal to the ceiling
            # grants no swap at all, and the throttle above stalls instead of
            # slowing. The allowance is the size of the request.
            swap_bytes=limit + reservation,
        )
        deferred[CGROUP_V2_MEMORY_HIGH_PARAMETER] = str(high_mib * MIB)
    return OciLinuxResources(cpu=cpu, memory=memory, deferred=deferred)


def _cpu_burst_ceiling_millicores(request_millicores: int, *, node_cpu_millicores: int) -> int:
    """How far above its request a container may run when the node is idle.

    Processor time is compressible, so this ceiling costs a neighbour latency
    rather than its life, and it can be generous. It still cannot exceed the
    machine: a quota above what the node has is not a larger allowance, it is an
    unenforceable number.
    """
    ceiling = request_millicores + CONTAINER_CPU_BURST_CEILING_MILLICORES
    if node_cpu_millicores <= 0:
        return ceiling
    # Floored at the request for the same reason memory is: shares still promise
    # the request under contention, so a quota below it would throttle a
    # container beneath what it reserved on an otherwise idle machine.
    return max(min(ceiling, schedulable_capacity(node_cpu_millicores)), request_millicores)


def _lowest_survivable_wall_mib(request_mib: int) -> int:
    """The smallest wall a container can reserve `request_mib` behind and live.

    The sandbox OOM watcher trips at a percentage of whatever wall it is handed,
    so a wall equal to the reservation is a kill *below* it: at ninety-five per
    cent, a container promised 4096 MiB dies at 3979 having never been throttled,
    inside the guarantee the reservation exists to sell. The wall therefore has to
    clear the reservation by at least the watcher's own margin.

    One mebibyte above the exact quotient, because the watcher truncates and the
    two must not meet.
    """
    exact = math.ceil(request_mib * 100 / DEFAULT_GVISOR_OOM_THRESHOLD_PERCENT)
    return exact + 1


def _hard_memory_ceiling_mib(high_mib: int, *, request_mib: int, node_memory_mib: int) -> int:
    """The wall behind the throttle, never larger than the machine holds.

    A container has to be able to reach its own ceiling for that ceiling to be
    the thing that stops it. Above the node's own size it never can, and the
    kernel's global OOM killer resolves the shortage instead -- by `oom_badness`,
    which scores resident size and knows nothing about what anyone reserved.

    Floored so the reservation stays survivable, which is a stronger floor than
    the reservation itself and the reason this is not `max(..., request_mib)`.
    Reaching that floor means spending headroom the overhead factor had set aside
    for the machine, and that is the right trade: a node too small to hold a
    request plus the watcher's margin was the wrong placement, and killing the
    tenant inside its reservation is not a better way to say so.
    """
    floor = _lowest_survivable_wall_mib(request_mib)
    if node_memory_mib <= 0:
        # The worker could not read its machine. Better a ceiling that may be too
        # generous than one invented from a number nobody measured.
        return max(high_mib, floor)
    return max(min(high_mib, schedulable_capacity(node_memory_mib)), floor)


def container_id_hash_suffix(container_id: str, length: int) -> str:
    if length <= 0:
        msg = "hash suffix length must be positive"
        raise ValueError(msg)
    encoded = hashlib.sha1(container_id.encode("utf-8")).hexdigest()
    return encoded[: min(length, len(encoded))]


def container_veth_names(
    container_id: str,
    *,
    host_prefix: str = "rth",
    container_prefix: str = "rtc",
    max_length: int = NETWORK_INTERFACE_NAME_MAX_LENGTH,
) -> tuple[str, str]:
    suffix_length = min(max_length - len(host_prefix), max_length - len(container_prefix))
    if suffix_length <= 0:
        msg = "veth prefixes are too long"
        raise ValueError(msg)
    suffix = container_id_hash_suffix(container_id, suffix_length)
    return (host_prefix + suffix, container_prefix + suffix)


def container_ipv4_host_offset(ip: str, subnet: str = DEFAULT_CONTAINER_SUBNET) -> int:
    network = ipaddress.ip_network(subnet, strict=False)
    address = ipaddress.ip_address(ip)
    if address.version != 4 or address not in network:
        msg = f"IPv4 address {ip} is outside container subnet {subnet}"
        raise ValueError(msg)
    return int(address) - int(network.network_address)


def container_ipv6_address(
    ipv4: str,
    *,
    ipv4_subnet: str = DEFAULT_CONTAINER_SUBNET,
    ipv6_subnet: str = DEFAULT_CONTAINER_IPV6_SUBNET,
) -> str:
    offset = container_ipv4_host_offset(ipv4, ipv4_subnet)
    network = ipaddress.ip_network(ipv6_subnet, strict=False)
    if network.version != 6:
        msg = "container IPv6 subnet must be IPv6"
        raise ValueError(msg)
    return str(ipaddress.ip_address(int(network.network_address) + offset))


def select_container_network(
    container_id: str,
    *,
    pod_address: str,
    persistent: bool = False,
    machine_id: str = "",
    transport: str = "",
    container_ip: str = "",
) -> ContainerNetworkSelection:
    normalized_transport = BackendRouteTransport(transport.strip()) if transport.strip() else None
    direct_transport = normalized_transport is BackendRouteTransport.Direct
    agent_bridge = (
        persistent
        and bool(machine_id)
        and normalized_transport is not None
        and not direct_transport
    )
    mode = NetworkAddressMode.AgentBridge if agent_bridge else NetworkAddressMode.LocalPod
    reason = ContainerNetworkSelectionReason.LocalDefault
    if direct_transport:
        reason = ContainerNetworkSelectionReason.DirectTransport
    if agent_bridge:
        reason = ContainerNetworkSelectionReason.AgentBridgePersistentMachine
    return ContainerNetworkSelection(
        identity=ContainerNetworkIdentity(
            container_id=container_id,
            pod_address=pod_address,
            container_ip=container_ip,
            mode=mode,
        ),
        persistent=persistent,
        machine_id=machine_id,
        transport=transport,
        reason=reason,
    )


def container_port_address_map(
    identity: ContainerNetworkIdentity,
    bindings: list[PortBinding],
) -> ContainerNetworkAddressMap:
    addresses: dict[int, str] = {}
    for binding in bindings:
        if identity.mode is NetworkAddressMode.AgentBridge:
            if not identity.container_ip:
                msg = f"container {identity.container_id} has no bridge IP"
                raise ValueError(msg)
            addresses[binding.container_port] = f"{identity.container_ip}:{binding.container_port}"
        else:
            if not identity.pod_address:
                msg = "pod address is empty"
                raise ValueError(msg)
            addresses[binding.container_port] = f"{identity.pod_address}:{binding.host_port}"
    return ContainerNetworkAddressMap(identity=identity, addresses=addresses)


def merge_path_members(existing: str, add: tuple[str, ...] | list[str]) -> str:
    members = [item for item in existing.split(":") if item] if existing else []
    for item in add:
        if item and item not in members:
            members.append(item)
    return ":".join(members)


def inject_nvidia_environment(
    image_env: list[str],
    *,
    host_env: dict[str, str] | None = None,
    default_cuda_version: str = DEFAULT_CUDA_VERSION,
) -> NvidiaEnvironmentPlan:
    host_env = host_env or {}
    values = env_list_to_map(image_env)
    cuda_version = _cuda_major_minor(values.get("CUDA_VERSION")) or default_cuda_version
    defaults = {
        "NVIDIA_DRIVER_CAPABILITIES": NVIDIA_DRIVER_CAPABILITIES,
        "NVIDIA_REQUIRE_CUDA": "",
        "NVARCH": "",
        "NV_CUDA_COMPAT_PACKAGE": "",
        "NV_CUDA_CUDART_VERSION": "",
        "CUDA_VERSION": "",
        "WORKER_GPU": "",
        "CUDA_HOME": f"/usr/local/cuda-{cuda_version}",
    }
    added: dict[str, str] = {}
    for key, default_value in defaults.items():
        if values.get(key):
            continue
        candidate = host_env.get(key, "") or default_value
        if candidate:
            values[key] = candidate
            added[key] = candidate
    values["PATH"] = merge_path_members(
        values.get("PATH", ""),
        (*DEFAULT_CONTAINER_PATHS, f"/usr/local/cuda-{cuda_version}/bin"),
    )
    values["LD_LIBRARY_PATH"] = merge_path_members(
        values.get("LD_LIBRARY_PATH", ""),
        (
            *DEFAULT_CONTAINER_LIBRARY_PATHS,
            f"/usr/local/cuda-{cuda_version}/targets/x86_64-linux/lib",
        ),
    )
    return NvidiaEnvironmentPlan(
        env=map_to_env_list(values),
        cuda_version=cuda_version,
        added_defaults=added,
    )


def plan_nvidia_mounts(driver_files: Mapping[str, str]) -> list[OciMount]:
    """Bind the driver's userspace to the paths a workload resolves it by.

    One mount per file rather than one for the directory they came from. That
    directory is the worker's own `/usr/lib`, where the NVIDIA runtime put them
    when the worker container started, and binding it whole would stack the
    worker's glibc in front of the workload's.

    Read-only: nothing in a container has business writing the driver, and the
    next container start reads the host afresh regardless.
    """
    return [
        OciMount(
            source=source,
            destination=destination,
            options=["rbind", "rprivate", "nosuid", "nodev", "ro"],
        )
        for destination, source in sorted(driver_files.items())
    ]


def nvidia_device_paths(assigned_devices: Sequence[int]) -> list[str]:
    """Every device node a container needs to reach the GPUs it was assigned."""
    return [
        *NVIDIA_CONTROL_DEVICE_PATHS,
        *(f"/dev/nvidia{index}" for index in sorted(set(assigned_devices))),
    ]


def plan_nvidia_devices(device_nodes: Sequence[DeviceNode]) -> list[OciDevice]:
    """Turn probed host device nodes into the spec entries a container needs.

    The numbers are read from the host rather than assumed: `nvidia` is a fixed
    major but `nvidia-uvm` is allocated dynamically at module load, so a hardcoded
    pair works on one machine and opens the wrong device on the next.

    Without these a GPU container gets the environment and the libraries and no
    device to open, which fails inside the workload as an unhelpful CUDA error
    rather than as a scheduling failure.
    """
    return [
        OciDevice(
            path=node.path,
            major=node.major,
            minor=node.minor,
            file_mode=node.file_mode,
        )
        for node in device_nodes
    ]


def plan_container_kill(container_id: str, *, kill: bool = False) -> RuntimeServerOperationPlan:
    return RuntimeServerOperationPlan(
        operation=ContainerRuntimeOperation.Kill,
        container_id=container_id,
        signal=9 if kill else 15,
        force_delete=True,
    )


def plan_container_exec(
    container_id: str,
    command: str,
    *,
    instance_env: list[str],
    request_env: list[str] | None = None,
    build_secret_env: list[str] | None = None,
    cwd: str = "/workspace",
    build_request: bool = False,
) -> RuntimeServerOperationPlan:
    env = [*instance_env, *(request_env or [])]
    if build_request:
        env.extend(build_secret_env or [])
    return RuntimeServerOperationPlan(
        operation=ContainerRuntimeOperation.Exec,
        container_id=container_id,
        argv=["sh", "-c", command],
        env=env,
        cwd=cwd,
        requires_running=True,
    )


def plan_sandbox_exec(
    container_id: str,
    command: str,
    *,
    instance_env: list[str],
    extra_env: dict[str, str] | None = None,
    cwd: str = "/workspace",
) -> RuntimeServerOperationPlan:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return RuntimeServerOperationPlan(
            operation=ContainerRuntimeOperation.SandboxExec,
            container_id=container_id,
            ok=False,
            error_message=str(exc),
            cwd=cwd,
        )
    env = [*instance_env, *map_to_env_list(extra_env or {})]
    return RuntimeServerOperationPlan(
        operation=ContainerRuntimeOperation.SandboxExec,
        container_id=container_id,
        argv=argv,
        env=env,
        cwd=cwd,
        requires_running=True,
    )


def plan_workspace_sync(
    container_id: str,
    *,
    workspace_root: str,
    operation: WorkspaceSyncOperation,
    path: str,
    new_path: str | None = None,
    is_dir: bool = False,
    data_size_bytes: int = 0,
) -> WorkspaceSyncPlan:
    target = _safe_workspace_path(workspace_root, path)
    planned_new_path = _safe_workspace_path(workspace_root, new_path) if new_path else None
    if operation is WorkspaceSyncOperation.Move and planned_new_path is None:
        msg = "new_path is required for move operations"
        raise ValueError(msg)
    if data_size_bytes < 0:
        msg = "data_size_bytes cannot be negative"
        raise ValueError(msg)
    return WorkspaceSyncPlan(
        container_id=container_id,
        operation=operation,
        workspace_root=workspace_root,
        target_path=target,
        new_path=planned_new_path,
        is_dir=is_dir,
        data_size_bytes=data_size_bytes,
    )


def stub_code_cache_key(workspace_id: str, object_id: str) -> str:
    payload = f"{len(workspace_id)}:{workspace_id}:{len(object_id)}:{object_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def checkpoint_accelerator(gpu: str | None) -> str:
    value = (gpu or "").strip()
    return value.upper() if value else "CPU"


def checkpoint_path(checkpoint_root: str, checkpoint_id: str) -> str:
    return posixpath.join(checkpoint_root.rstrip("/"), checkpoint_id)


def checkpoint_archive_path(checkpoint_root: str, checkpoint_id: str) -> str:
    return posixpath.join(
        checkpoint_root.rstrip("/"),
        checkpoint_id + CHECKPOINT_ARCHIVE_EXTENSION,
    )


def select_worker_oom_watcher(
    runtime: OciRuntimeName,
    *,
    pid: int,
    memory_enforced: bool,
    memory_limit_bytes: int | None = None,
    cgroup_path: str | None = None,
) -> WorkerOomWatcherPlan:
    if pid <= 0:
        return WorkerOomWatcherPlan(runtime=runtime, enabled=False, reason="pid is unavailable")
    if runtime is OciRuntimeName.Runsc:
        if not memory_enforced:
            return WorkerOomWatcherPlan(
                runtime=runtime,
                enabled=False,
                reason="sandbox memory enforcement is disabled",
            )
        if not memory_limit_bytes:
            return WorkerOomWatcherPlan(
                runtime=runtime,
                enabled=False,
                reason="sandbox memory limit is unavailable",
            )
        return WorkerOomWatcherPlan(
            runtime=runtime,
            enabled=True,
            watcher=OomWatcherKind.ProcessMemory,
            memory_limit_bytes=memory_limit_bytes,
            stop_container_on_oom=True,
            reason="watch sandbox process memory",
        )
    if not cgroup_path:
        return WorkerOomWatcherPlan(
            runtime=runtime,
            enabled=False,
            reason="cgroup path is unavailable",
        )
    return WorkerOomWatcherPlan(
        runtime=runtime,
        enabled=True,
        watcher=OomWatcherKind.Cgroup,
        cgroup_path=cgroup_path,
        stop_container_on_oom=False,
        reason="watch cgroup OOM counter",
    )


def _cuda_major_minor(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split(".")
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[1]}"


def _safe_workspace_path(root: str, relative_path: str | None) -> str:
    if relative_path is None:
        msg = "workspace path is required"
        raise ValueError(msg)
    root_clean = "/" + root.strip("/")
    target = posixpath.normpath(posixpath.join(root_clean, relative_path.lstrip("/")))
    if target != root_clean and not target.startswith(root_clean + "/"):
        msg = "workspace sync path escapes workspace root"
        raise ValueError(msg)
    return target
