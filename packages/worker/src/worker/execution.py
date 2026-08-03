from __future__ import annotations

import hashlib
import ipaddress
import posixpath
import shlex
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import Field, JsonValue, field_validator
from shared.container_requests import CONTAINER_INNER_PORT
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

from worker.runtime_config import OciRuntimeName, OomWatcherKind, RuntimeCapabilities

CGROUP_V2_OOM_GROUP_PARAMETER = "memory.oom.group"
DEFAULT_CGROUP_V2_PARAMETERS: dict[str, str] = {CGROUP_V2_OOM_GROUP_PARAMETER: "1"}
DEFAULT_CPU_SHARE_UNIT = 1024
DEFAULT_CPU_PERIOD_US = 100_000
DEFAULT_MEMORY_OVERHEAD_FACTOR = 1.25
# A request is a floor, not a ceiling: shares guarantee it under contention
# while quota is set well above it so idle worker capacity is usable. The
# ceiling exists only to stop one container monopolising a machine.
DEFAULT_CPU_BURST_CEILING_MILLICORES = 16_000
# Memory gets the same floor-plus-ceiling treatment. Unlike Modal this keeps a
# hard ceiling rather than leaving memory unbounded: an unbounded container
# that leaks is reaped by the worker-wide OOM killer, which picks an arbitrary
# victim, whereas a per-container ceiling kills the container responsible and
# keeps the OOM watcher's attribution correct.
DEFAULT_MEMORY_BURST_CEILING_MIB = 8_192
DEFAULT_CUDA_VERSION = "12.4"
DEFAULT_CONTAINER_PATHS = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
DEFAULT_CONTAINER_LIBRARY_PATHS = (
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/worker/x86_64-linux-gnu",
    "/usr/local/nvidia/lib64",
)
NVIDIA_DRIVER_CAPABILITIES = "compute,utility,graphics,ngx,video"
NETWORK_INTERFACE_NAME_MAX_LENGTH = 15
NETWORK_SLOT_PREFIX = "network-slot"
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
    Status = "status"
    Checkpoint = "checkpoint"
    Archive = "archive"
    WorkspaceSync = "workspace-sync"
    SandboxExec = "sandbox-exec"


class WorkspaceSyncOperation(StrEnum):
    Delete = "delete"
    Write = "write"
    Move = "move"


class ContainerMountKind(StrEnum):
    Local = "local"
    WorkspaceStorage = "workspace-storage"
    ObjectMount = "object-mount"
    UserCode = "user-code"
    UserOutput = "user-output"


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
    unified: dict[str, str] = Field(default_factory=dict)

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
        if self.unified:
            payload["unified"] = dict(self.unified)
        return payload


class ContainerResourceRequest(ContractModel):
    cpu_millicores: int
    memory_mib: int
    memory_enforced: bool = True
    cgroup_v2_oom_group: bool = True
    cpu_share_unit: int = DEFAULT_CPU_SHARE_UNIT
    cpu_period_us: int = DEFAULT_CPU_PERIOD_US
    cpu_burst_ceiling_millicores: int = DEFAULT_CPU_BURST_CEILING_MILLICORES
    memory_overhead_factor: float = DEFAULT_MEMORY_OVERHEAD_FACTOR
    memory_burst_ceiling_mib: int = DEFAULT_MEMORY_BURST_CEILING_MIB

    @field_validator("cpu_millicores", "memory_mib", "cpu_share_unit", "cpu_period_us")
    @classmethod
    def resource_numbers_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "container resource values must be positive"
            raise ValueError(msg)
        return value

    @field_validator("memory_overhead_factor")
    @classmethod
    def overhead_must_be_at_least_one(cls, value: float) -> float:
        if value < 1:
            msg = "memory overhead factor must be at least 1"
            raise ValueError(msg)
        return value


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


class ContainerNetworkSlotPlan(ContractModel):
    slot_id: str
    worker_id: str = ""
    reservation_id: str
    veth_host: str
    veth_container: str
    ipv4: str | None = None
    ipv6: str | None = None


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
    checkpoint_id: str | None = None
    progress_keepalive_seconds: int | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class WorkspaceSyncPlan(ContractModel):
    container_id: str
    operation: WorkspaceSyncOperation
    workspace_root: str
    target_path: str
    new_path: str | None = None
    is_dir: bool = False
    data_size_bytes: int = 0


class ContainerMount(ContractModel):
    mount_path: str
    local_path: str = ""
    kind: ContainerMountKind = ContainerMountKind.Local


class WorkspaceStorageMountDecision(ContractModel):
    required: bool
    reason: str
    code_cache_key: str | None = None
    adjusted_mounts: list[ContainerMount] = Field(default_factory=list)


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


class CheckpointMaterializationPlan(ContractModel):
    checkpoint_id: str
    checkpoint_path: str
    archive_path: str
    origin_key: str
    cache_hash: str
    expected_size_bytes: int
    filesystem_payload_path: str
    temporary_extract_root: str


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
    ceiling_millicores = request.cpu_millicores + request.cpu_burst_ceiling_millicores
    cpu = OciLinuxCpu(
        shares=request.cpu_millicores * request.cpu_share_unit // 1000,
        quota=ceiling_millicores * request.cpu_period_us // 1000,
        period=request.cpu_period_us,
    )
    unified = (
        dict(DEFAULT_CGROUP_V2_PARAMETERS)
        if request.memory_enforced and request.cgroup_v2_oom_group
        else {}
    )
    memory: OciLinuxMemory | None = None
    if request.memory_enforced:
        # reservation is the request, so the container is protected under memory
        # pressure. The limit is a burst ceiling rather than the request plus a
        # fixed overhead, so a small request can still use idle worker memory
        # instead of being pinned a few percent above what it asked for.
        reservation = request.memory_mib * 1024 * 1024
        overhead_limit = int(reservation * request.memory_overhead_factor)
        burst_limit = reservation + request.memory_burst_ceiling_mib * 1024 * 1024
        limit = max(overhead_limit, burst_limit)
        memory = OciLinuxMemory(
            reservation_bytes=reservation,
            limit_bytes=limit,
            swap_bytes=limit,
        )
    return OciLinuxResources(cpu=cpu, memory=memory, unified=unified)


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


def network_slot_reservation_id(slot_id: str, worker_id: str = "") -> str:
    if not slot_id:
        msg = "slot_id is required"
        raise ValueError(msg)
    parts = [NETWORK_SLOT_PREFIX]
    if worker_id:
        parts.append(worker_id)
    parts.append(slot_id)
    return ":".join(parts)


def parse_network_slot_reservation_id(value: str) -> tuple[str, str, bool]:
    prefix = f"{NETWORK_SLOT_PREFIX}:"
    if not value.startswith(prefix):
        return ("", "", False)
    rest = value[len(prefix) :]
    if not rest:
        return ("", "", False)
    parts = rest.split(":")
    slot_id = parts[-1]
    if not slot_id:
        return ("", "", False)
    worker_id = parts[0] if len(parts) > 1 else ""
    return (worker_id, slot_id, True)


def container_ipv4_address_count(subnet: str = DEFAULT_CONTAINER_SUBNET) -> int:
    network = ipaddress.ip_network(subnet, strict=False)
    if network.version != 4:
        msg = "container subnet must be IPv4"
        raise ValueError(msg)
    return network.num_addresses


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


def plan_network_slot(
    container_id: str,
    slot_id: str,
    *,
    worker_id: str = "",
    ipv4: str | None = None,
) -> ContainerNetworkSlotPlan:
    veth_host, veth_container = container_veth_names(container_id)
    return ContainerNetworkSlotPlan(
        slot_id=slot_id,
        worker_id=worker_id,
        reservation_id=network_slot_reservation_id(slot_id, worker_id),
        veth_host=veth_host,
        veth_container=veth_container,
        ipv4=ipv4,
        ipv6=container_ipv6_address(ipv4) if ipv4 else None,
    )


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


def plan_nvidia_mounts(
    existing_host_paths: set[str],
    *,
    default_cuda_version: str = DEFAULT_CUDA_VERSION,
) -> list[OciMount]:
    candidates = (f"/usr/local/cuda-{default_cuda_version}", "/usr/local/nvidia/lib64")
    return [
        OciMount(
            source=path,
            destination=path,
            options=["rbind", "rprivate", "nosuid", "nodev", "rw"],
        )
        for path in candidates
        if path in existing_host_paths
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


def plan_container_status(
    container_id: str,
    *,
    runtime_status: str | None,
) -> RuntimeServerOperationPlan:
    running = runtime_status == "running"
    return RuntimeServerOperationPlan(
        operation=ContainerRuntimeOperation.Status,
        container_id=container_id,
        ok=running,
        metadata={"running": running, "runtime_status": runtime_status or "unknown"},
    )


def plan_container_checkpoint(
    container_id: str,
    capabilities: RuntimeCapabilities,
    *,
    checkpoint_id: str,
) -> RuntimeServerOperationPlan:
    if not capabilities.checkpoint_restore:
        return RuntimeServerOperationPlan(
            operation=ContainerRuntimeOperation.Checkpoint,
            container_id=container_id,
            ok=False,
            error_message="runtime does not support checkpoint/restore",
        )
    return RuntimeServerOperationPlan(
        operation=ContainerRuntimeOperation.Checkpoint,
        container_id=container_id,
        checkpoint_id=checkpoint_id,
    )


def plan_archive_progress(
    container_id: str,
    *,
    progress: int = 0,
    done: bool = False,
    success: bool = False,
    keepalive_seconds: int = 10,
    error_message: str = "",
) -> RuntimeServerOperationPlan:
    if not 0 <= progress <= 100:
        msg = "archive progress must be between 0 and 100"
        raise ValueError(msg)
    return RuntimeServerOperationPlan(
        operation=ContainerRuntimeOperation.Archive,
        container_id=container_id,
        ok=success if done else True,
        error_message=error_message,
        requires_running=True,
        progress_keepalive_seconds=keepalive_seconds,
        metadata={"progress": progress, "done": done, "success": success},
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


def requires_workspace_storage_mount(
    *,
    storage_available: bool,
    is_build_request: bool,
    direct_code_download_available: bool,
    workspace_id: str,
    object_id: str,
    mounts: list[ContainerMount],
) -> WorkspaceStorageMountDecision:
    if not storage_available:
        return WorkspaceStorageMountDecision(required=False, reason="workspace storage unavailable")
    if is_build_request:
        return WorkspaceStorageMountDecision(
            required=True,
            reason="build request requires workspace storage",
            code_cache_key=stub_code_cache_key(workspace_id, object_id),
            adjusted_mounts=mounts,
        )
    for mount in mounts:
        if mount.kind is ContainerMountKind.UserCode and not direct_code_download_available:
            return WorkspaceStorageMountDecision(
                required=True,
                reason="user code mount requires workspace storage",
                code_cache_key=stub_code_cache_key(workspace_id, object_id),
                adjusted_mounts=mounts,
            )
        if mount.kind in {ContainerMountKind.WorkspaceStorage, ContainerMountKind.UserOutput}:
            return WorkspaceStorageMountDecision(
                required=True,
                reason=f"{mount.kind.value} mount requires workspace storage",
                adjusted_mounts=mounts,
            )
    return WorkspaceStorageMountDecision(required=False, reason="no workspace storage mount needed")


def checkpoint_origin_key(checkpoint_id: str) -> str:
    return posixpath.join(CHECKPOINT_ORIGIN_PREFIX, checkpoint_id + CHECKPOINT_ARCHIVE_EXTENSION)


def checkpoint_accelerator(gpu: str | None) -> str:
    value = (gpu or "").strip()
    return value.upper() if value else "CPU"


def build_checkpoint_cache_metadata(
    *,
    checkpoint_id: str,
    cache_hash: str,
    size_bytes: int,
    locality: str = "",
    gpu: str | None = None,
) -> CheckpointCacheMetadata:
    return CheckpointCacheMetadata(
        cache_hash=cache_hash,
        size_bytes=size_bytes,
        origin_key=checkpoint_origin_key(checkpoint_id),
        locality=locality,
        accelerator=checkpoint_accelerator(gpu),
    )


def checkpoint_path(checkpoint_root: str, checkpoint_id: str) -> str:
    return posixpath.join(checkpoint_root.rstrip("/"), checkpoint_id)


def checkpoint_archive_path(checkpoint_root: str, checkpoint_id: str) -> str:
    return posixpath.join(
        checkpoint_root.rstrip("/"),
        checkpoint_id + CHECKPOINT_ARCHIVE_EXTENSION,
    )


def build_checkpoint_materialization_plan(
    *,
    checkpoint_root: str,
    checkpoint_id: str,
    metadata: CheckpointCacheMetadata,
) -> CheckpointMaterializationPlan:
    target_path = checkpoint_path(checkpoint_root, checkpoint_id)
    return CheckpointMaterializationPlan(
        checkpoint_id=checkpoint_id,
        checkpoint_path=target_path,
        archive_path=checkpoint_archive_path(checkpoint_root, checkpoint_id),
        origin_key=metadata.origin_key,
        cache_hash=metadata.cache_hash,
        expected_size_bytes=metadata.size_bytes,
        filesystem_payload_path=posixpath.join(target_path, CHECKPOINT_FILESYSTEM_DIR),
        temporary_extract_root=posixpath.join(
            posixpath.dirname(target_path),
            f".{checkpoint_id}.extract",
        ),
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
