from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator
from shared.capacity import CapacityOwnerIdentity
from shared.contracts import ContractModel
from shared.routing import BackendRouteTransport

from compute.telemetry import (
    AgentMachineMetrics,
    AgentMachineStatus,
    AgentTelemetryState,
    agent_machine_connected,
    agent_machine_last_seen,
    agent_machine_status,
)

DEFAULT_PRIVATE_TRANSPORT = "tsnet_restricted"
DEFAULT_PRIVATE_FALLBACK = "internal"
DEFAULT_PRIVATE_PRIORITY = 1000


class ComputePoolMode(StrEnum):
    Private = "private"


class PrivatePoolFallback(StrEnum):
    Internal = "internal"
    Wait = "wait"
    Fail = "fail"


class WorkerStatus(StrEnum):
    Pending = "pending"
    Available = "available"
    Disabled = "disabled"
    Unknown = "unknown"


class MachineStatus(StrEnum):
    Registered = "registered"
    Pending = "pending"
    Available = "available"
    Disabled = "disabled"


class ComputePoolSource(StrEnum):
    Attached = "attached"
    Managed = "managed"


class PoolConfig(ContractModel):
    name: str
    selector: str = ""
    mode: ComputePoolMode | str = ComputePoolMode.Private
    transport: BackendRouteTransport | str = ""
    fallback: PrivatePoolFallback | str = ""
    priority: int = 0
    gpu: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    nodes: int = 0
    offer_id: str = ""
    ttl: str = ""
    max_spend: float = 0.0
    min_reliability: float = 0.0

    @field_validator("nodes")
    @classmethod
    def nodes_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "nodes cannot be negative"
            raise ValueError(msg)
        return value


class NormalizedPoolConfig(ContractModel):
    name: str
    selector: str = ""
    mode: ComputePoolMode = ComputePoolMode.Private
    transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted
    fallback: PrivatePoolFallback = PrivatePoolFallback.Internal
    priority: int = DEFAULT_PRIVATE_PRIORITY
    gpu: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    nodes: int = 0
    offer_id: str = ""
    ttl: str = ""
    max_spend: float = 0.0
    min_reliability: float = 0.0

    @field_validator("nodes")
    @classmethod
    def nodes_cannot_be_negative(cls, value: int) -> int:
        return PoolConfig.nodes_cannot_be_negative(value)


class ComputePoolPlan(ContractModel):
    name: str
    selector: str
    gpu: list[str] = Field(default_factory=list)
    nodes: int
    offer_id: str = ""
    ttl_seconds: int = 0
    max_spend_micros: int = 0
    providers: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    min_reliability: float = 0.0


class ProviderReservation(ContractModel):
    id: str
    pool_name: str = ""
    selector: str = ""
    provider: str = ""
    cloud: str = ""
    region: str = ""
    offer_id: str = ""
    instance_id: str = ""
    status: str = ""
    gpu: str | None = None
    gpu_count: int = 0
    hourly_cost_micros: int = 0
    committed_micros: int = 0
    source: ComputePoolSource | str = ComputePoolSource.Attached
    created_at: datetime | None = None
    expires_at: datetime | None = None
    billing_renewal_at: datetime | None = None
    billing_cursor_at: datetime | None = None
    last_status_check_at: datetime | None = None
    last_billing_check_at: datetime | None = None
    status_message: str = ""
    terminating_reason: str = ""
    last_error: str = ""
    registration_token_hash: str = ""
    machine_id: str = ""
    node_count: int = 0
    instance_type: str = ""
    cpu_millicores: int = 0
    memory_mb: int = 0
    storage_mb: int = 0
    architecture: str = ""
    runtime: str = ""


class ProviderInstanceProjection(ContractModel):
    id: str
    pool_name: str = ""
    provider: str = ""
    cloud: str = ""
    region: str = ""
    offer_id: str = ""
    status: str = ""
    gpu_count: int = 0
    hourly_cost_micros: int = 0
    source: str = ""
    created_at: str = ""
    expires_at: str = ""
    billing_renewal_at: str = ""
    status_message: str = ""
    terminating_reason: str = ""
    machine_id: str = ""
    node_count: int = 0
    instance_type: str = ""
    cpu_millicores: int = 0
    memory_mb: int = 0
    storage_mb: int = 0


class PrivatePoolState(CapacityOwnerIdentity):
    workspace_id: str = ""
    name: str
    selector: str = ""
    config: PoolConfig | None = None
    reservations: list[ProviderReservation] = Field(default_factory=list)
    committed_spend_micros: int = 0
    status: str = ""
    source: ComputePoolSource | str = ComputePoolSource.Attached
    created_by_token_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    reserved_nodes: int = 0


class PrivatePoolProjection(ContractModel):
    name: str
    selector: str
    config: NormalizedPoolConfig | None = None
    reservations: list[ProviderInstanceProjection] = Field(default_factory=list)
    committed_spend_micros: int = 0
    status: str = ""
    source: str = ""
    created_at: str = ""
    expires_at: str = ""
    machine_count: int = 0
    ready_machine_count: int = 0
    reserved_nodes: int = 0


class WorkerCapacityState(ContractModel):
    status: WorkerStatus = WorkerStatus.Unknown
    machine_id: str = ""
    pool_name: str = ""
    total_cpu_millicores: int = 0
    free_cpu_millicores: int = 0
    total_memory_mb: int = 0
    free_memory_mb: int = 0
    total_gpu_count: int = 0
    free_gpu_count: int = 0
    active_container_ids: list[str] = Field(default_factory=list)


class MachineProjection(ContractModel):
    id: str
    cpu_millicores: int = 0
    memory_mb: int = 0
    gpu: str = ""
    gpu_count: int = 0
    status: MachineStatus
    pool_name: str = ""
    provider_name: str = "agent"
    created: str = ""
    last_keepalive: str = ""
    metrics: AgentMachineMetrics


def normalize_backend_route_transport(value: str) -> BackendRouteTransport:
    normalized = value.strip()
    if normalized == "":
        return BackendRouteTransport.TsnetRestricted
    return BackendRouteTransport(normalized)


def normalize_pool_config(config: PoolConfig | None) -> NormalizedPoolConfig | None:
    if config is None:
        return None
    if str(config.mode or ComputePoolMode.Private.value) != ComputePoolMode.Private.value:
        msg = f"private pool mode must be {ComputePoolMode.Private.value!r}"
        raise ValueError(msg)
    return NormalizedPoolConfig(
        name=config.name,
        selector=config.selector or config.name,
        mode=ComputePoolMode.Private,
        transport=normalize_backend_route_transport(str(config.transport)),
        fallback=PrivatePoolFallback(str(config.fallback or DEFAULT_PRIVATE_FALLBACK)),
        priority=config.priority or DEFAULT_PRIVATE_PRIORITY,
        gpu=config.gpu,
        providers=config.providers,
        regions=config.regions,
        nodes=config.nodes,
        offer_id=config.offer_id,
        ttl=config.ttl,
        max_spend=config.max_spend,
        min_reliability=config.min_reliability,
    )


def compute_pool_from_config(
    config: PoolConfig | None,
    *,
    node_count: int = 0,
    require_reservation: bool = False,
) -> ComputePoolPlan:
    if config is None:
        msg = "pool config is required"
        raise ValueError(msg)
    normalized = normalize_pool_config(config)
    if normalized is None:
        msg = "pool config is required"
        raise ValueError(msg)
    if normalized.transport is not BackendRouteTransport.TsnetRestricted:
        msg = f"unsupported agent transport {normalized.transport.value!r}"
        raise ValueError(msg)
    if normalized.fallback not in set(PrivatePoolFallback):
        msg = f"unsupported private pool fallback {normalized.fallback!s}"
        raise ValueError(msg)
    ttl_seconds = parse_ttl_seconds(normalized.ttl)
    if normalized.min_reliability < 0 or normalized.min_reliability > 1:
        msg = "min_reliability must be between 0 and 1"
        raise ValueError(msg)
    gpu_types = sorted({gpu for gpu in normalized.gpu if gpu})
    if require_reservation and len(gpu_types) > 1:
        msg = "private pool reservations require a single GPU type"
        raise ValueError(msg)
    nodes = node_count or normalized.nodes
    if require_reservation and nodes <= 0:
        msg = "nodes must be positive for reservation pools"
        raise ValueError(msg)
    max_spend_micros = dollars_to_micros(normalized.max_spend)
    if require_reservation and nodes > 0 and ttl_seconds <= 0:
        msg = "pool reservations require ttl"
        raise ValueError(msg)
    if require_reservation and nodes > 0 and max_spend_micros <= 0:
        msg = "pool reservations require max_spend"
        raise ValueError(msg)
    return ComputePoolPlan(
        name=normalized.name,
        selector=normalized.selector,
        gpu=gpu_types,
        nodes=nodes,
        offer_id=normalized.offer_id,
        ttl_seconds=ttl_seconds,
        max_spend_micros=max_spend_micros,
        providers=normalized.providers,
        regions=normalized.regions,
        min_reliability=normalized.min_reliability,
    )


def validate_pool_resource_compatibility(
    existing: PrivatePoolState | None,
    request: ComputePoolPlan,
) -> None:
    if existing is None:
        return
    existing_gpu = _pool_gpu_types(existing)
    request_gpu = sorted({gpu for gpu in request.gpu if gpu})
    if existing.reserved_nodes > 0 and not existing_gpu and request_gpu:
        msg = (
            f"pool {existing.name!r} is configured for CPU-only nodes; "
            "create a separate pool for GPU nodes"
        )
        raise ValueError(msg)
    if existing_gpu and request.nodes > 0 and not request_gpu:
        msg = (
            f"pool {existing.name!r} is configured for GPU nodes; "
            "create a separate pool for CPU-only nodes"
        )
        raise ValueError(msg)
    if len(existing_gpu) > 1:
        msg = f"pool {existing.name!r} already has mixed GPU types: {', '.join(existing_gpu)}"
        raise ValueError(msg)
    if len(request_gpu) > 1:
        msg = "private pool reservations require a single GPU type"
        raise ValueError(msg)
    if existing_gpu and request_gpu and existing_gpu[0] != request_gpu[0]:
        msg = (
            f"pool {existing.name!r} is configured for GPU type {existing_gpu[0]!r}; "
            f"create a separate pool for GPU type {request_gpu[0]!r}"
        )
        raise ValueError(msg)


def project_provider_instance(
    reservation: ProviderReservation,
    *,
    cost_multiplier: float = 1.0,
) -> ProviderInstanceProjection:
    return ProviderInstanceProjection(
        id=reservation.id,
        pool_name=reservation.pool_name,
        provider=reservation.provider,
        cloud=reservation.cloud,
        region=reservation.region,
        offer_id=reservation.offer_id,
        status=reservation.status,
        gpu_count=reservation.gpu_count,
        hourly_cost_micros=int(reservation.hourly_cost_micros * cost_multiplier),
        source=(
            reservation.source.value
            if isinstance(reservation.source, ComputePoolSource)
            else str(reservation.source)
        ),
        created_at=format_compute_time(reservation.created_at),
        expires_at=format_compute_time(reservation.expires_at),
        billing_renewal_at=format_compute_time(reservation.billing_renewal_at),
        status_message=reservation.status_message,
        terminating_reason=reservation.terminating_reason,
        machine_id=reservation.machine_id,
        node_count=reservation.node_count,
        instance_type=reservation.instance_type,
        cpu_millicores=reservation.cpu_millicores,
        memory_mb=reservation.memory_mb,
        storage_mb=reservation.storage_mb,
    )


def project_private_pool(
    state: PrivatePoolState | None,
    *,
    machines: list[AgentTelemetryState] | None = None,
    now: datetime | None = None,
    billable_margin_pct: float = 0.10,
) -> PrivatePoolProjection | None:
    if state is None:
        return None
    machine_states = machines or []
    cost_multiplier = 1.0 + billable_margin_pct
    source = (
        state.source.value if isinstance(state.source, ComputePoolSource) else str(state.source)
    )
    return PrivatePoolProjection(
        name=state.name,
        selector=state.selector,
        config=normalize_pool_config(state.config),
        reservations=[
            project_provider_instance(reservation, cost_multiplier=cost_multiplier)
            for reservation in state.reservations
        ],
        committed_spend_micros=state.committed_spend_micros,
        status=state.status,
        source=source,
        created_at=format_compute_time(state.created_at),
        expires_at=format_compute_time(state.expires_at),
        machine_count=len(machine_states),
        ready_machine_count=sum(
            1 for machine in machine_states if agent_machine_connected(machine, now=now)
        ),
        reserved_nodes=state.reserved_nodes,
    )


def project_agent_machine(
    state: AgentTelemetryState | None,
    *,
    worker: WorkerCapacityState | None = None,
    now: datetime | None = None,
) -> MachineProjection:
    if state is None:
        return MachineProjection(
            id="",
            status=MachineStatus.Registered,
            metrics=AgentMachineMetrics(),
        )
    last_seen = agent_machine_last_seen(state)
    return MachineProjection(
        id=state.machine_id,
        cpu_millicores=state.cpu_millicores,
        memory_mb=state.memory_mb,
        gpu=",".join(state.gpus),
        gpu_count=state.gpu_count,
        status=agent_machine_public_status(state, worker=worker, now=now),
        pool_name=state.pool_name,
        created=format_compute_time(state.last_join_at),
        last_keepalive=format_compute_time(last_seen),
        metrics=project_agent_machine_metrics(state, worker=worker),
    )


def project_agent_machine_metrics(
    state: AgentTelemetryState,
    *,
    worker: WorkerCapacityState | None = None,
) -> AgentMachineMetrics:
    total_cpu = state.cpu_millicores
    total_memory = state.metrics.memory_total_mb or state.memory_mb
    if worker is not None:
        total_cpu = worker.total_cpu_millicores or total_cpu
        total_memory = worker.total_memory_mb or total_memory
        return state.metrics.model_copy(
            update={
                "cpu_utilization_pct": capacity_utilization_pct(
                    worker.total_cpu_millicores,
                    worker.free_cpu_millicores,
                ),
                "memory_utilization_pct": capacity_utilization_pct(
                    worker.total_memory_mb,
                    worker.free_memory_mb,
                ),
                "memory_used_mb": max(worker.total_memory_mb - worker.free_memory_mb, 0),
                "memory_total_mb": total_memory,
                "worker_count": 1,
                "container_count": len(worker.active_container_ids),
                "free_gpu_count": worker.free_gpu_count,
            }
        )
    return state.metrics.model_copy(
        update={
            "memory_total_mb": total_memory,
            "free_gpu_count": state.metrics.free_gpu_count,
        }
    )


def agent_machine_public_status(
    state: AgentTelemetryState,
    *,
    worker: WorkerCapacityState | None = None,
    now: datetime | None = None,
) -> MachineStatus:
    if agent_machine_status(state, now=now) is AgentMachineStatus.PreflightFail:
        return MachineStatus.Disabled
    if not agent_machine_connected(state, now=now):
        return MachineStatus.Registered
    if worker is None:
        return MachineStatus.Registered
    if worker.status is WorkerStatus.Available:
        return MachineStatus.Available
    if worker.status is WorkerStatus.Disabled:
        return MachineStatus.Disabled
    return MachineStatus.Pending


def capacity_utilization_pct(total: int, free: int) -> float:
    if total <= 0:
        return 0.0
    used = max(total - free, 0)
    return used * 100 / total


def dollars_to_micros(value: float) -> int:
    return int(value * 1_000_000)


def parse_ttl_seconds(value: str) -> int:
    raw = value.strip()
    if raw == "":
        return 0
    if raw.isdigit():
        return int(raw)
    match = _TTL_PATTERN.fullmatch(raw)
    if match is None:
        msg = f"invalid ttl: {value}"
        raise ValueError(msg)
    amount = int(match.group("amount"))
    unit = match.group("unit")
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def format_compute_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat().replace("+00:00", "Z")


_TTL_PATTERN = re.compile(r"(?P<amount>[1-9][0-9]*)(?P<unit>[smhd])")


def _pool_gpu_types(state: PrivatePoolState) -> list[str]:
    configured = sorted({gpu for gpu in (state.config.gpu if state.config else []) if gpu})
    if configured:
        return configured
    observed = sorted(
        {
            reservation.gpu or ""
            for reservation in state.reservations
            if reservation.gpu_count > 0 and reservation.gpu
        }
    )
    return [gpu for gpu in observed if gpu]
