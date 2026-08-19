from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import Field, JsonValue
from shared.app_identity import PRIVATE_RESOURCE_PREFIX
from shared.compute_policy import MachinePool
from shared.contracts import ContractModel
from shared.timestamps import utc_now

from compute.state import ComputeAgentTokenState

TELEMETRY_CREDENTIAL_TIMEOUT_SECONDS = 10.0
AGENT_HEARTBEAT_TIMEOUT_SECONDS = 60.0
AGENT_HEARTBEAT_FUTURE_TOLERANCE_SECONDS = 5.0
DEFAULT_TELEMETRY_STREAM_PREFIX = "events"
TELEMETRY_REDACTED_VALUE = "redacted"


class TelemetryCredentialKind(StrEnum):
    Logs = "logs"
    Events = "events"


class TelemetryCredentialOperation(StrEnum):
    Append = "append"


class TelemetrySinkStatus(StrEnum):
    Disabled = "disabled"
    Planned = "planned"
    Ready = "ready"


class AgentTelemetryDecisionKind(StrEnum):
    Accept = "accept"
    InvalidAgentToken = "invalid-agent-token"
    AgentTokenChanged = "agent-token-changed"


class AgentMachineStatus(StrEnum):
    Schedulable = "schedulable"
    Disconnected = "disconnected"
    PreflightFail = "preflight-fail"


class AgentDisconnectAction(StrEnum):
    Ignore = "ignore"
    MarkDisconnected = "mark-disconnected"
    AlreadyMarked = "already-marked"


class NodeType(StrEnum):
    BringYourOwn = "byo"
    Managed = "managed"


class CapacitySource(StrEnum):
    Attached = "attached"
    Managed = "managed"


class PoolMode(StrEnum):
    Private = "private"
    Shared = "shared"


class TelemetryRootStreamConfig(ContractModel):
    api_key: str = ""
    basin: str = ""
    stream_prefix: str = DEFAULT_TELEMETRY_STREAM_PREFIX

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip() and self.basin.strip())

    @property
    def normalized_stream_prefix(self) -> str:
        return normalize_stream_prefix(self.stream_prefix)


class TelemetryCredentialScope(ContractModel):
    basin_exact: str
    stream_prefix: str
    operations: tuple[TelemetryCredentialOperation, ...] = (TelemetryCredentialOperation.Append,)


class TelemetryCredentialIssuePlan(ContractModel):
    credential_id: str
    workspace_id: str
    kind: TelemetryCredentialKind
    scope: TelemetryCredentialScope
    request_timeout_seconds: float = TELEMETRY_CREDENTIAL_TIMEOUT_SECONDS


class AgentTelemetrySinkConfig(ContractModel):
    destination: str
    credential: str
    stream_prefix: str


class AgentScopedTelemetryConfig(ContractModel):
    enabled: bool = False
    stream_prefix: str = DEFAULT_TELEMETRY_STREAM_PREFIX
    logs: AgentTelemetrySinkConfig | None = None
    events: AgentTelemetrySinkConfig | None = None


class AgentScopedTelemetryPlan(ContractModel):
    status: TelemetrySinkStatus
    workspace_id: str
    stream_prefix: str = DEFAULT_TELEMETRY_STREAM_PREFIX
    issue_plans: tuple[TelemetryCredentialIssuePlan, ...] = ()
    config: AgentScopedTelemetryConfig = Field(default_factory=AgentScopedTelemetryConfig)
    reason: str = ""


class TelemetryCredentialIssuer(Protocol):
    def issue(self, plan: TelemetryCredentialIssuePlan) -> str: ...


class AgentMetricSnapshot(Protocol):
    timestamp_unix_nano: int
    cpu_utilization_pct: float
    memory_used_mb: int
    memory_total_mb: int
    memory_utilization_pct: float
    disk_used_mb: int
    disk_total_mb: int
    disk_usage_pct: float
    disk_path: str
    network_recv_bytes: int
    network_sent_bytes: int
    network_recv_packets: int
    network_sent_packets: int
    worker_count: int
    container_count: int
    free_gpu_count: int


class AgentTelemetryStreamDecision(ContractModel):
    kind: AgentTelemetryDecisionKind
    accepted: bool
    agent_token: str = ""
    reason: str = ""


class AgentMachineMetrics(ContractModel):
    timestamp: datetime = Field(default_factory=utc_now)
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


class AgentTelemetryState(ContractModel):
    workspace_id: str
    pool: MachinePool
    machine_id: str
    executor: str = ""
    os: str = ""
    arch: str = ""
    hostname: str = ""
    cpu_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    gpus: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    gpu_count: int = 0
    schedulable: bool = True
    preflight_error: bool = False
    last_join_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    metrics: AgentMachineMetrics = Field(default_factory=AgentMachineMetrics)


def agent_telemetry_state(state: ComputeAgentTokenState) -> AgentTelemetryState:
    return AgentTelemetryState(
        workspace_id=state.workspace_id,
        pool=state.pool,
        machine_id=state.machine_id,
        executor=state.executor,
        os=state.os,
        arch=state.arch,
        hostname=state.hostname,
        cpu_count=state.cpu_count,
        cpu_millicores=state.cpu_millicores,
        memory_mb=state.memory_mb,
        gpus=state.gpus,
        gpu_ids=state.gpu_ids,
        gpu_count=state.gpu_count,
        schedulable=state.schedulable,
        preflight_error=any(not item.ok for item in state.preflight),
        last_join_at=state.last_join_at,
        last_heartbeat_at=state.last_heartbeat_at,
        last_disconnect_at=state.last_disconnect_at,
    )


class PoolTelemetryState(ContractModel):
    source: CapacitySource = CapacitySource.Attached
    mode: PoolMode = PoolMode.Private
    transport: str = ""


class AgentMetricUpdatePlan(ContractModel):
    accepted: bool
    reason: str = ""
    metrics: AgentMachineMetrics
    heartbeat_at: datetime
    last_disconnect_at: datetime | None = None
    node_usage_seconds: float = 0.0
    node_usage_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    event_status: AgentMachineStatus = AgentMachineStatus.Disconnected
    event_attrs: dict[str, str] = Field(default_factory=dict)
    should_register_pool: bool = False


class AgentDisconnectPlan(ContractModel):
    """What to do about a machine that stopped reporting.

    Nothing here disables the machine's worker. The scheduler's agent-pool
    controller does that from the same heartbeat rule on every pass, and a
    second disabler reached on a different cadence would be two owners for one
    transition. This plan owns the durable record and the telling.
    """

    action: AgentDisconnectAction
    disconnected_at: datetime | None = None
    reason: str = ""


def normalize_stream_prefix(value: str) -> str:
    prefix = value.strip().strip("/")
    return prefix or DEFAULT_TELEMETRY_STREAM_PREFIX


def telemetry_stream_part(value: str) -> str:
    return value.strip().strip("/").replace("/", "_")


def telemetry_credential_id(
    workspace_id: str,
    kind: TelemetryCredentialKind | str,
    *,
    suffix: bytes | None = None,
) -> str:
    kind_value = kind.value if isinstance(kind, TelemetryCredentialKind) else str(kind)
    digest = hashlib.sha256(workspace_id.encode()).digest()
    random_suffix = suffix if suffix is not None else _credential_suffix(digest)
    return f"{PRIVATE_RESOURCE_PREFIX}-{kind_value}-{digest[:6].hex()}-{random_suffix[:6].hex()}"


def plan_scoped_telemetry_credentials(
    config: TelemetryRootStreamConfig,
    workspace_id: str,
    *,
    suffixes: dict[TelemetryCredentialKind, bytes] | None = None,
) -> AgentScopedTelemetryPlan:
    stream_prefix = config.normalized_stream_prefix
    if not config.enabled:
        return AgentScopedTelemetryPlan(
            status=TelemetrySinkStatus.Disabled,
            workspace_id=workspace_id,
            stream_prefix=stream_prefix,
            reason="root telemetry stream credentials are not configured",
        )

    workspace_part = telemetry_stream_part(workspace_id)
    prefixes = {
        TelemetryCredentialKind.Logs: f"{stream_prefix}/logs/workspaces/{workspace_part}",
        TelemetryCredentialKind.Events: f"{stream_prefix}/workspaces/{workspace_part}",
    }
    plans = tuple(
        TelemetryCredentialIssuePlan(
            credential_id=telemetry_credential_id(
                workspace_id,
                kind,
                suffix=(suffixes or {}).get(kind),
            ),
            workspace_id=workspace_id,
            kind=kind,
            scope=TelemetryCredentialScope(
                basin_exact=config.basin,
                stream_prefix=prefixes[kind],
            ),
        )
        for kind in (TelemetryCredentialKind.Logs, TelemetryCredentialKind.Events)
    )
    return AgentScopedTelemetryPlan(
        status=TelemetrySinkStatus.Planned,
        workspace_id=workspace_id,
        stream_prefix=stream_prefix,
        issue_plans=plans,
    )


def build_scoped_telemetry_config(
    plan: AgentScopedTelemetryPlan,
    issued_credentials: dict[TelemetryCredentialKind, str],
) -> AgentScopedTelemetryPlan:
    if plan.status is TelemetrySinkStatus.Disabled:
        return plan
    by_kind = {item.kind: item for item in plan.issue_plans}
    if any(kind not in issued_credentials for kind in by_kind):
        return plan.model_copy(update={"reason": "not all scoped telemetry credentials issued"})
    config = AgentScopedTelemetryConfig(
        enabled=True,
        stream_prefix=plan.stream_prefix,
        logs=AgentTelemetrySinkConfig(
            destination=by_kind[TelemetryCredentialKind.Logs].scope.basin_exact,
            credential=issued_credentials[TelemetryCredentialKind.Logs],
            stream_prefix=by_kind[TelemetryCredentialKind.Logs].scope.stream_prefix,
        ),
        events=AgentTelemetrySinkConfig(
            destination=by_kind[TelemetryCredentialKind.Events].scope.basin_exact,
            credential=issued_credentials[TelemetryCredentialKind.Events],
            stream_prefix=by_kind[TelemetryCredentialKind.Events].scope.stream_prefix,
        ),
    )
    return plan.model_copy(
        update={"status": TelemetrySinkStatus.Ready, "config": config, "reason": ""}
    )


def validate_agent_telemetry_token(
    request_token: str,
    *,
    current_token: str | None = None,
    state_found: bool = True,
) -> AgentTelemetryStreamDecision:
    token = request_token.strip()
    if not token or not state_found:
        return AgentTelemetryStreamDecision(
            kind=AgentTelemetryDecisionKind.InvalidAgentToken,
            accepted=False,
            reason="invalid agent token",
        )
    if current_token is not None and token != current_token:
        return AgentTelemetryStreamDecision(
            kind=AgentTelemetryDecisionKind.AgentTokenChanged,
            accepted=False,
            agent_token=current_token,
            reason="agent token changed on telemetry stream",
        )
    return AgentTelemetryStreamDecision(
        kind=AgentTelemetryDecisionKind.Accept,
        accepted=True,
        agent_token=token,
    )


def redact_telemetry_line(line: str) -> str:
    redacted = line
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def time_from_unix_nano(value: int, *, now: datetime | None = None) -> datetime:
    current = now or utc_now()
    if value <= 0:
        return current
    timestamp = datetime.fromtimestamp(value / 1_000_000_000, UTC)
    if timestamp > current:
        return current
    return timestamp


def machine_metrics_from_snapshot(
    snapshot: AgentMetricSnapshot,
    *,
    now: datetime | None = None,
) -> AgentMachineMetrics:
    return AgentMachineMetrics(
        timestamp=time_from_unix_nano(snapshot.timestamp_unix_nano, now=now),
        cpu_utilization_pct=snapshot.cpu_utilization_pct,
        memory_used_mb=snapshot.memory_used_mb,
        memory_total_mb=snapshot.memory_total_mb,
        memory_utilization_pct=snapshot.memory_utilization_pct,
        disk_used_mb=snapshot.disk_used_mb,
        disk_total_mb=snapshot.disk_total_mb,
        disk_usage_pct=snapshot.disk_usage_pct,
        disk_path=snapshot.disk_path,
        network_recv_bytes=snapshot.network_recv_bytes,
        network_sent_bytes=snapshot.network_sent_bytes,
        network_recv_packets=snapshot.network_recv_packets,
        network_sent_packets=snapshot.network_sent_packets,
        worker_count=snapshot.worker_count,
        container_count=snapshot.container_count,
        free_gpu_count=snapshot.free_gpu_count,
    )


def plan_agent_metric_update(
    state: AgentTelemetryState,
    snapshot: AgentMetricSnapshot,
    *,
    gateway_now: datetime | None = None,
    previous_seen: datetime | None = None,
    pool: PoolTelemetryState | None = None,
    capacity_metrics: AgentMachineMetrics | None = None,
) -> AgentMetricUpdatePlan:
    now = gateway_now or utc_now()
    metrics = machine_metrics_from_snapshot(snapshot, now=now)
    heartbeat_at = state.last_heartbeat_at
    if heartbeat_at is None or now > heartbeat_at:
        heartbeat_at = now
    effective_capacity = capacity_metrics or metrics
    updated_state = state.model_copy(
        update={
            "last_heartbeat_at": heartbeat_at,
            "last_disconnect_at": None,
            "metrics": metrics,
        }
    )
    node_seconds = node_usage_seconds(previous_seen or agent_machine_last_seen(state), now)
    usage_metadata = agent_node_usage_metadata(
        updated_state,
        pool=pool,
        metrics=effective_capacity,
        usage_seconds=node_seconds,
    )
    return AgentMetricUpdatePlan(
        accepted=True,
        metrics=metrics,
        heartbeat_at=heartbeat_at,
        last_disconnect_at=None,
        node_usage_seconds=node_seconds,
        node_usage_metadata=usage_metadata,
        event_status=agent_machine_status(updated_state, now=now),
        event_attrs=machine_heartbeat_event_attrs(metrics, effective_capacity),
        should_register_pool=pool is not None,
    )


def agent_node_usage_metadata(
    state: AgentTelemetryState,
    *,
    pool: PoolTelemetryState | None = None,
    metrics: AgentMachineMetrics | None = None,
    usage_seconds: float,
) -> dict[str, JsonValue]:
    pool_state = pool or PoolTelemetryState()
    node_type = (
        NodeType.BringYourOwn if pool_state.source is CapacitySource.Attached else NodeType.Managed
    )
    effective = metrics or state.metrics
    return {
        "workspace_id": state.workspace_id,
        "pool": state.pool,
        "machine_id": state.machine_id,
        "node_type": node_type.value,
        "capacity_source": pool_state.source.value,
        "pool_mode": pool_state.mode.value,
        "transport": pool_state.transport,
        "executor": state.executor,
        "os": state.os,
        "arch": state.arch,
        "hostname": state.hostname,
        "cpu_count": state.cpu_count,
        "cpu_millicores": state.cpu_millicores,
        "memory_mb": state.memory_mb,
        "gpu": ",".join(state.gpus),
        "gpu_ids": ",".join(state.gpu_ids),
        "gpu_count": state.gpu_count,
        "usage_seconds": usage_seconds,
        "worker_count": effective.worker_count,
        "container_count": effective.container_count,
        "free_gpu_count": effective.free_gpu_count,
        "cpu_used_pct": effective.cpu_utilization_pct,
        "memory_used_pct": effective.memory_utilization_pct,
        "cache_used_pct": state.metrics.disk_usage_pct,
        "cache_total_mb": state.metrics.disk_total_mb,
        "cache_used_mb": state.metrics.disk_used_mb,
        "host_memory_used_mb": state.metrics.memory_used_mb,
        "network_recv_bytes": state.metrics.network_recv_bytes,
        "network_sent_bytes": state.metrics.network_sent_bytes,
        "network_recv_packets": state.metrics.network_recv_packets,
        "network_sent_packets": state.metrics.network_sent_packets,
    }


def machine_heartbeat_event_attrs(
    host_metrics: AgentMachineMetrics,
    capacity_metrics: AgentMachineMetrics | None = None,
) -> dict[str, str]:
    capacity = capacity_metrics or host_metrics
    return {
        "cpu_utilization_pct": _format_float(capacity.cpu_utilization_pct),
        "memory_used_mb": str(capacity.memory_used_mb),
        "memory_utilization_pct": _format_float(capacity.memory_utilization_pct),
        "host_cpu_utilization_pct": _format_float(host_metrics.cpu_utilization_pct),
        "host_memory_used_mb": str(host_metrics.memory_used_mb),
        "host_memory_utilization_pct": _format_float(host_metrics.memory_utilization_pct),
        "disk_used_mb": str(host_metrics.disk_used_mb),
        "disk_total_mb": str(host_metrics.disk_total_mb),
        "disk_usage_pct": _format_float(host_metrics.disk_usage_pct),
        "disk_path": host_metrics.disk_path,
        "network_recv_bytes": str(host_metrics.network_recv_bytes),
        "network_sent_bytes": str(host_metrics.network_sent_bytes),
        "network_recv_packets": str(host_metrics.network_recv_packets),
        "network_sent_packets": str(host_metrics.network_sent_packets),
        "worker_count": str(capacity.worker_count),
        "container_count": str(capacity.container_count),
        "free_gpu_count": str(capacity.free_gpu_count),
    }


def node_usage_seconds(
    previous_seen: datetime | None,
    current_seen: datetime | None,
    *,
    heartbeat_timeout_seconds: float = AGENT_HEARTBEAT_TIMEOUT_SECONDS,
) -> float:
    if previous_seen is None or current_seen is None or current_seen <= previous_seen:
        return 0.0
    elapsed = (current_seen - previous_seen).total_seconds()
    return min(elapsed, heartbeat_timeout_seconds)


def agent_machine_last_seen(state: AgentTelemetryState) -> datetime | None:
    if state.last_heartbeat_at is not None and (
        state.last_join_at is None or state.last_heartbeat_at > state.last_join_at
    ):
        return state.last_heartbeat_at
    return state.last_join_at


def agent_silence_description(
    state: AgentTelemetryState,
    *,
    now: datetime | None = None,
) -> str:
    """How long this machine has been quiet, coarsely, for a person to read.

    Coarse on purpose: the number goes in a message somebody reads to decide
    whether to go and look at a host, and to the second it would imply the
    platform knows the moment the machine stopped. It knows the last time one
    arrived.

    Empty when the machine has never reported, which is a different sentence.
    """

    last_seen = agent_machine_last_seen(state)
    if last_seen is None:
        return ""
    elapsed = ((now or utc_now()) - last_seen).total_seconds()
    if elapsed < 60:
        return _plural(max(int(elapsed), 0), "second")
    if elapsed < 3600:
        return _plural(int(elapsed // 60), "minute")
    if elapsed < 86400:
        return _plural(int(elapsed // 3600), "hour")
    return _plural(int(elapsed // 86400), "day")


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def agent_machine_connected(
    state: AgentTelemetryState,
    *,
    now: datetime | None = None,
) -> bool:
    if not state.schedulable:
        return False
    current = now or utc_now()
    last_seen = agent_machine_last_seen(state)
    if last_seen is None:
        return False
    if state.last_disconnect_at is not None and state.last_disconnect_at >= last_seen:
        return False
    if last_seen > current:
        return (last_seen - current).total_seconds() <= AGENT_HEARTBEAT_FUTURE_TOLERANCE_SECONDS
    return (current - last_seen).total_seconds() <= AGENT_HEARTBEAT_TIMEOUT_SECONDS


def agent_machine_status(
    state: AgentTelemetryState,
    *,
    now: datetime | None = None,
) -> AgentMachineStatus:
    if agent_machine_connected(state, now=now):
        return AgentMachineStatus.Schedulable
    if not state.schedulable and state.preflight_error:
        return AgentMachineStatus.PreflightFail
    return AgentMachineStatus.Disconnected


def plan_agent_disconnect(
    state: AgentTelemetryState,
    *,
    now: datetime | None = None,
) -> AgentDisconnectPlan:
    current = now or utc_now()
    last_seen = agent_machine_last_seen(state)
    if last_seen is None:
        return AgentDisconnectPlan(action=AgentDisconnectAction.Ignore, reason="never-seen")
    if last_seen >= current - timedelta(seconds=AGENT_HEARTBEAT_TIMEOUT_SECONDS):
        return AgentDisconnectPlan(action=AgentDisconnectAction.Ignore, reason="heartbeat-fresh")
    if state.last_disconnect_at is not None and state.last_disconnect_at >= last_seen:
        return AgentDisconnectPlan(
            action=AgentDisconnectAction.AlreadyMarked,
            reason="disconnect-already-marked",
        )
    return AgentDisconnectPlan(
        action=AgentDisconnectAction.MarkDisconnected,
        disconnected_at=current,
        reason="heartbeat-stale",
    )


def _credential_suffix(digest: bytes) -> bytes:
    try:
        return os.urandom(6)
    except OSError:
        return digest[-6:]


def _format_float(value: float) -> str:
    return f"{value:.2f}"


_SENSITIVE_KEY_PATTERN = (
    r"[A-Za-z0-9_.-]*(?:access[_-]?key|accesskey|api[_-]?key|apikey|"
    r"secret|token|password|credentials?|credential)[A-Za-z0-9_.-]*"
)
_REDACTION_PATTERNS = [
    (
        re.compile(
            r'(?i)(["\']?authorization["\']?\s*[:=]\s*)("?)'
            r"(bearer|basic)(\s+)([A-Za-z0-9._~+/=-]+)(\"?)"
        ),
        rf"\1\2\3\4{TELEMETRY_REDACTED_VALUE}\6",
    ),
    (
        re.compile(r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9._~+/=-]+)"),
        rf"\1\2{TELEMETRY_REDACTED_VALUE}",
    ),
    (
        re.compile(r"(?i)\b(api\s*key|auth\s*key|token)(\s+)([A-Za-z0-9._~+/=-]+)"),
        rf"\1\2{TELEMETRY_REDACTED_VALUE}",
    ),
    (
        re.compile(
            r"(?i)([\"']?"
            + _SENSITIVE_KEY_PATTERN
            + r"[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"
        ),
        rf"\1{TELEMETRY_REDACTED_VALUE}",
    ),
]
