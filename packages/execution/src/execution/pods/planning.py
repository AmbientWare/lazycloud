from __future__ import annotations

from enum import StrEnum

from compute.resources import normalize_gpu_count
from pydantic import Field, computed_field
from shared.autoscaling import PodStubType
from shared.contracts import ContractModel
from shared.env import GATEWAY_TOKEN_ENV, STUB_ID_ENV, STUB_TYPE_ENV
from shared.workload_keys import pod_keep_warm_lock_key

POD_CONTAINER_PREFIX = "pod"
SANDBOX_CONTAINER_PREFIX = "sandbox"
DEFAULT_POD_CONNECTION_TIMEOUT_SECONDS = 600
DEFAULT_POD_PROXY_BUFFER_SIZE = 300
POD_BUFFER_PROCESSING_INTERVAL_MS = 50
POD_CONTAINER_DISCOVERY_INTERVAL_MS = 250
POD_CONTAINER_DIAL_TIMEOUT_SECONDS = 30
POD_CONNECTION_KEEPALIVE_SECONDS = 1
POD_CONNECTION_READ_TIMEOUT_SECONDS = 300
POD_CONTAINER_AVAILABLE_TIMEOUT_SECONDS = 2
POD_TCP_HANDLER_KEY_TTL_SECONDS = 300


class PodContainerEnvVar(StrEnum):
    GatewayToken = GATEWAY_TOKEN_ENV
    StubId = STUB_ID_ENV
    StubType = STUB_TYPE_ENV
    KeepWarmSeconds = "KEEP_WARM_SECONDS"


class PodContainerStartRequest(ContractModel):
    workspace_name: str
    workspace_id: str
    app_id: str = ""
    stub_id: str
    stub_type: PodStubType = PodStubType.PodDeployment
    gateway_token: str = ""
    keep_warm_seconds: int = Field(default=0, ge=-1)
    container_id: str | None = None
    container_id_suffix: str = "00000000"
    entrypoint: list[str] = Field(default_factory=list)
    cpu_millicores: int = Field(default=0, ge=0)
    cpu_limit_millicores: int = Field(default=0, ge=0)
    memory_mib: int = Field(default=0, ge=0)
    memory_limit_mib: int = Field(default=0, ge=0)
    disk_mib: int = Field(default=0, ge=0)
    requires_gpu: bool = False
    gpu_count: int = Field(default=0, ge=0)
    gpu_request: list[str] = Field(default_factory=list)
    image_id: str = ""
    env: list[str] = Field(default_factory=list)
    secret_env: list[str] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)
    checkpoint_enabled: bool = False


class PodContainerStartPlan(ContractModel):
    container_id: str
    entrypoint: list[str]
    env: list[str]
    cpu_millicores: int
    cpu_limit_millicores: int = 0
    memory_mib: int
    memory_limit_mib: int = 0
    disk_mib: int = 0
    gpu_count: int
    gpu_request: list[str]
    image_id: str
    ports: list[int]
    checkpoint_enabled: bool
    keep_warm_lock_key: str | None = None
    keep_warm_lock_ttl_seconds: int | None = None


class PodProxyProtocol(StrEnum):
    Http = "http"
    Tcp = "tcp"
    WebSocket = "websocket"


class PodProxyFailureReason(StrEnum):
    NoAvailableContainers = "no-available-containers"
    PortUnavailable = "port-unavailable"


class PodBackendContainer(ContractModel):
    container_id: str
    address_map: dict[int, str] = Field(default_factory=dict)
    active_connections: int = Field(default=0, ge=0)
    ready: bool = True


class PodProxyRequest(ContractModel):
    port: int = Field(ge=1, le=65535)
    container_id: str | None = None
    sub_path: str = ""
    query_string: str = ""
    protocol: PodProxyProtocol = PodProxyProtocol.Http


class PodProxyPlan(ContractModel):
    protocol: PodProxyProtocol
    container_id: str | None = None
    target_host: str | None = None
    url: str | None = None
    failure_reason: PodProxyFailureReason | None = None
    backend_dial_timeout_seconds: int = POD_CONTAINER_DIAL_TIMEOUT_SECONDS
    connection_keepalive_seconds: int = POD_CONNECTION_KEEPALIVE_SECONDS
    connection_read_timeout_seconds: int = POD_CONNECTION_READ_TIMEOUT_SECONDS

    @computed_field
    @property
    def available(self) -> bool:
        return self.failure_reason is None


class PodTcpSniPlan(ContractModel):
    sni: str
    handler_cache_key: str
    handler_cache_ttl_seconds: int = POD_TCP_HANDLER_KEY_TTL_SECONDS
    handler_path: str = ""
    cache_handler_path: bool = False
    forward_to_pod: bool = False


def pod_instance_lock_key(workspace_id: str, stub_id: str) -> str:
    return f"pod:{workspace_id}:{stub_id}:instance_lock"


def pod_tcp_sni_handler_key(sni: str) -> str:
    return f"middleware:tcp_sni:{sni}:handler"


def pod_container_id(stub_type: PodStubType, stub_id: str, suffix: str) -> str:
    prefix = SANDBOX_CONTAINER_PREFIX if stub_type is PodStubType.Sandbox else POD_CONTAINER_PREFIX
    return f"{prefix}-{stub_id}-{suffix}"


def plan_pod_container_start(request: PodContainerStartRequest) -> PodContainerStartPlan:
    container_id = request.container_id or pod_container_id(
        request.stub_type,
        request.stub_id,
        request.container_id_suffix,
    )
    gpu_count = normalize_gpu_count(request.requires_gpu, request.gpu_count)
    return PodContainerStartPlan(
        container_id=container_id,
        entrypoint=request.entrypoint,
        env=[
            *request.secret_env,
            *request.env,
            f"{PodContainerEnvVar.GatewayToken.value}={request.gateway_token}",
            f"{PodContainerEnvVar.StubId.value}={request.stub_id}",
            f"{PodContainerEnvVar.StubType.value}={request.stub_type.value}",
            f"{PodContainerEnvVar.KeepWarmSeconds.value}={request.keep_warm_seconds}",
        ],
        cpu_millicores=request.cpu_millicores,
        cpu_limit_millicores=request.cpu_limit_millicores,
        memory_mib=request.memory_mib,
        memory_limit_mib=request.memory_limit_mib,
        disk_mib=request.disk_mib,
        gpu_count=gpu_count,
        gpu_request=request.gpu_request,
        image_id=request.image_id,
        ports=request.ports,
        checkpoint_enabled=request.checkpoint_enabled,
        keep_warm_lock_key=(
            pod_keep_warm_lock_key(
                request.workspace_id,
                request.stub_id,
                container_id,
            )
            if request.keep_warm_seconds != 0
            else None
        ),
        keep_warm_lock_ttl_seconds=(
            request.keep_warm_seconds if request.keep_warm_seconds > 0 else None
        ),
    )


def plan_pod_proxy(
    request: PodProxyRequest,
    containers: list[PodBackendContainer],
) -> PodProxyPlan:
    ready = [
        container
        for container in containers
        if container.ready
        and (request.container_id is None or container.container_id == request.container_id)
    ]
    if not ready:
        return PodProxyPlan(
            protocol=request.protocol,
            failure_reason=PodProxyFailureReason.NoAvailableContainers,
        )
    port_ready = [container for container in ready if request.port in container.address_map]
    if not port_ready:
        container = min(ready, key=lambda item: item.active_connections)
        return PodProxyPlan(
            protocol=request.protocol,
            container_id=container.container_id,
            failure_reason=PodProxyFailureReason.PortUnavailable,
        )
    container = min(port_ready, key=lambda item: item.active_connections)
    target_host = container.address_map.get(request.port)
    if target_host is None:
        return PodProxyPlan(
            protocol=request.protocol,
            container_id=container.container_id,
            failure_reason=PodProxyFailureReason.PortUnavailable,
        )
    path = request.sub_path
    if path and not path.startswith("/"):
        path = f"/{path}"
    query = f"?{request.query_string}" if request.query_string else ""
    return PodProxyPlan(
        protocol=request.protocol,
        container_id=container.container_id,
        target_host=target_host,
        url=(
            f"http://{target_host}{path}{query}"
            if request.protocol is not PodProxyProtocol.Tcp
            else None
        ),
    )


def plan_pod_tcp_sni(
    *,
    sni: str,
    handler_path: str = "",
    stub_type: PodStubType | None = None,
    version: int = 0,
    stub_id: str = "",
) -> PodTcpSniPlan:
    return PodTcpSniPlan(
        sni=sni,
        handler_cache_key=pod_tcp_sni_handler_key(sni),
        handler_path=handler_path,
        cache_handler_path=bool(handler_path and (version > 0 or stub_id)),
        forward_to_pod=stub_type
        in {PodStubType.Pod, PodStubType.PodDeployment, PodStubType.PodRun},
    )
