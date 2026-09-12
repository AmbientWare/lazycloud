from __future__ import annotations

from compute.agent_control import AgentBootstrapConfig
from pydantic import Field, JsonValue, field_validator
from pydantic.json_schema import SkipJsonSchema
from shared.bytes_transport import decode_bytes, encode_bytes
from shared.capacity import CAPACITY_OWNER_ID_PATTERN
from shared.compute_enrollment import (
    AgentCapacityState,
    AgentWorkerSlotStatus,
    ComputePreflightCheck,
)
from shared.compute_policy import MachinePool
from shared.http.base import HttpModel
from shared.identity import AuthScope
from shared.routing import (
    BackendRouteKind,
    BackendRouteProtocol,
    BackendRouteState,
    BackendRouteTransport,
)
from shared.usage import UsageBillingOwner


class AuthorizeRequest(HttpModel):
    action: AuthScope = AuthScope.Read


class AuthorizeResponse(HttpModel):
    workspace_id: str = ""


class SignPayloadRequest(HttpModel):
    payload_base64: str = ""
    workspace: str = "default"
    timestamp: int | None = None

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        workspace: str = "default",
        timestamp: int | None = None,
    ) -> SignPayloadRequest:
        return cls(
            payload_base64=encode_bytes(payload),
            workspace=workspace,
            timestamp=timestamp,
        )

    def payload_bytes(self) -> bytes:
        return decode_bytes(self.payload_base64)


class SignPayloadResponse(HttpModel):
    signature: str = ""
    timestamp: int = 0


class JoinAgentRequest(HttpModel):
    join_token: str
    machine_fingerprint: str = ""
    hostname: str = ""
    os: str = ""
    arch: str = ""
    cpu_count: int = 0
    memory_mb: int = 0
    gpu: list[str] = Field(default_factory=list)
    gpu_count: int = 0
    preflight: list[ComputePreflightCheck] = Field(default_factory=list)
    schedulable: bool = True
    executor: str = ""
    cpu_millicores: int = 0
    gpu_ids: list[str] = Field(default_factory=list)


class JoinAgentResponse(HttpModel):
    workspace_id: str = ""
    pool: MachinePool = MachinePool("")
    machine_id: str = ""
    agent_token: str = ""
    credential_id: str = ""
    credential_generation: int = Field(default=1, ge=1)
    capacity_state: AgentCapacityState = AgentCapacityState.Available
    bootstrap: AgentBootstrapConfig | None = None


class LeaveAgentRequest(HttpModel):
    agent_token: str
    machine_id: str = ""
    cache_generation_id: str = ""
    cache_session_fence: int | None = Field(default=None, ge=1)


class LeaveAgentResponse(HttpModel):
    machine_id: str


class AgentRoute(HttpModel):
    route_id: str
    workspace_id: str
    pool: MachinePool
    machine_id: str
    worker_id: str = ""
    container_id: str = ""
    kind: BackendRouteKind = BackendRouteKind.Container
    port: int = 0
    protocol: BackendRouteProtocol = BackendRouteProtocol.Tcp
    transport: BackendRouteTransport = BackendRouteTransport.PrivateNetwork
    local_target: str = ""
    proxy_target: str = ""
    state: BackendRouteState = BackendRouteState.Opening
    error: str = ""
    updated_at: int = 0
    proxy_auth_token: str = Field(default="", repr=False)


class ListAgentRoutesRequest(HttpModel):
    agent_token: str


class ListAgentRoutesResponse(HttpModel):
    routes: list[AgentRoute] = Field(default_factory=list)


class UpdateAgentRouteStatusRequest(HttpModel):
    agent_token: str
    route_id: str
    state: BackendRouteState | None = None
    proxy_target: str = ""
    error: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)

    @field_validator("state", mode="before")
    @classmethod
    def empty_state_is_unchanged(
        cls,
        value: BackendRouteState | JsonValue,
    ) -> BackendRouteState | JsonValue:
        if value == "":
            return None
        return value


class UpdateAgentRouteStatusResponse(HttpModel):
    route_id: str = ""


class AgentWorkerSlot(HttpModel):
    worker_id: str
    worker_token: str = ""
    pool: MachinePool = MachinePool("")
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    billing_owner: UsageBillingOwner
    machine_id: str = ""
    cpu: int = 0
    memory: int = 0
    gpu: str = ""
    gpu_count: int = 0
    gpu_assignment: str = ""
    network_prefix: str = ""
    worker_image: str = ""
    status: AgentWorkerSlotStatus = AgentWorkerSlotStatus.Active


class StreamAgentRequest(HttpModel):
    agent_token: str
    generation: SkipJsonSchema[int] = Field(default=0, ge=0, exclude=True)
    binary_sha256: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")
    active_worker_images: dict[str, str] = Field(default_factory=dict)
    prepared_worker_images: list[str] = Field(default_factory=list)


class StreamAgentResponse(HttpModel):
    ok: bool = True
    generation: SkipJsonSchema[int] = Field(default=0, ge=0, exclude=True)
    err_msg: str = ""
    retryable: bool = False
    credential_id: str = ""
    credential_generation: int = Field(default=1, ge=1)
    capacity_state: AgentCapacityState = AgentCapacityState.Available
    bootstrap: AgentBootstrapConfig | None = None
    routes: list[AgentRoute] = Field(default_factory=list)
    slots: list[AgentWorkerSlot] = Field(default_factory=list)


class AgentLogRecord(HttpModel):
    source: str = ""
    worker_id: str = ""
    level: str = ""
    stream: str = ""
    line: str = ""
    timestamp_unix_nano: int = 0


class AgentMetricSnapshot(HttpModel):
    timestamp_unix_nano: int = 0
    cpu_utilization_pct: float = 0.0
    memory_used_mb: int = 0
    memory_total_mb: int = 0
    memory_utilization_pct: float = 0.0
    disk_used_mb: int = 0
    disk_total_mb: int = 0
    disk_usage_pct: float = 0.0
    disk_path: str = "/"
    network_recv_bytes: int = 0
    network_sent_bytes: int = 0
    network_recv_packets: int = 0
    network_sent_packets: int = 0
    worker_count: int = 0
    container_count: int = 0
    free_gpu_count: int = 0


class AgentEventRecord(HttpModel):
    action: str = ""
    status: str = ""
    message: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)
    timestamp_unix_nano: int = 0
    event_type: str = ""


class AgentTelemetryRequest(HttpModel):
    agent_token: str
    logs: list[AgentLogRecord] = Field(default_factory=list)
    metrics: AgentMetricSnapshot | None = None
    events: list[AgentEventRecord] = Field(default_factory=list)


class AgentTelemetryResponse(HttpModel):
    ok: bool = True
    err_msg: str = ""


__all__ = [
    "AgentBootstrapConfig",
    "AgentEventRecord",
    "AgentLogRecord",
    "AgentMetricSnapshot",
    "AgentRoute",
    "AgentTelemetryRequest",
    "AgentTelemetryResponse",
    "AgentWorkerSlot",
    "AuthorizeRequest",
    "AuthorizeResponse",
    "JoinAgentRequest",
    "JoinAgentResponse",
    "LeaveAgentRequest",
    "LeaveAgentResponse",
    "ListAgentRoutesRequest",
    "ListAgentRoutesResponse",
    "SignPayloadRequest",
    "SignPayloadResponse",
    "StreamAgentRequest",
    "StreamAgentResponse",
    "UpdateAgentRouteStatusRequest",
    "UpdateAgentRouteStatusResponse",
]
