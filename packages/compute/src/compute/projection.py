from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator
from shared.capacity import CapacityOwnerIdentity
from shared.compute_policy import (
    MachinePool,
    UnitName,
)
from shared.contracts import ContractModel
from shared.routing import BackendRouteTransport, PrivateUnitFallback

DEFAULT_PRIVATE_FALLBACK = "internal"


class ComputeUnitMode(StrEnum):
    Private = "private"


class ComputeUnitSource(StrEnum):
    Attached = "attached"
    Managed = "managed"


class PoolConfig(ContractModel):
    name: str
    selector: str = ""
    mode: ComputeUnitMode | str = ComputeUnitMode.Private
    transport: BackendRouteTransport | str = ""
    fallback: PrivateUnitFallback | str = ""
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


class NormalizedUnitConfig(ContractModel):
    name: str
    selector: str = ""
    mode: ComputeUnitMode = ComputeUnitMode.Private
    transport: BackendRouteTransport = BackendRouteTransport.PrivateNetwork
    fallback: PrivateUnitFallback = PrivateUnitFallback.Internal
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
        return PoolConfig.nodes_cannot_be_negative(value)


class ComputeUnitPlan(ContractModel):
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
    pool: MachinePool = MachinePool("")
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
    source: ComputeUnitSource | str = ComputeUnitSource.Attached
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


class PrivateUnitState(CapacityOwnerIdentity):
    workspace_id: str = ""
    name: UnitName
    pool: MachinePool = MachinePool("")
    """Pool this unit's machines serve, so the agent bootstrap never has to
    substitute the unit's name for the label it must advertise."""
    selector: str = ""
    config: PoolConfig | None = None
    reservations: list[ProviderReservation] = Field(default_factory=list)
    committed_spend_micros: int = 0
    status: str = ""
    source: ComputeUnitSource | str = ComputeUnitSource.Attached
    created_by_token_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    reserved_nodes: int = 0


def normalize_backend_route_transport(value: str) -> BackendRouteTransport:
    normalized = value.strip()
    if normalized == "":
        return BackendRouteTransport.PrivateNetwork
    return BackendRouteTransport(normalized)


def normalize_unit_config(config: PoolConfig | None) -> NormalizedUnitConfig | None:
    if config is None:
        return None
    if str(config.mode or ComputeUnitMode.Private.value) != ComputeUnitMode.Private.value:
        msg = f"private pool mode must be {ComputeUnitMode.Private.value!r}"
        raise ValueError(msg)
    return NormalizedUnitConfig(
        name=config.name,
        selector=config.selector or config.name,
        mode=ComputeUnitMode.Private,
        transport=normalize_backend_route_transport(str(config.transport)),
        fallback=PrivateUnitFallback(str(config.fallback or DEFAULT_PRIVATE_FALLBACK)),
        priority=config.priority,
        gpu=config.gpu,
        providers=config.providers,
        regions=config.regions,
        nodes=config.nodes,
        offer_id=config.offer_id,
        ttl=config.ttl,
        max_spend=config.max_spend,
        min_reliability=config.min_reliability,
    )


def compute_unit_from_config(
    config: PoolConfig | None,
    *,
    node_count: int = 0,
    require_reservation: bool = False,
) -> ComputeUnitPlan:
    if config is None:
        msg = "pool config is required"
        raise ValueError(msg)
    normalized = normalize_unit_config(config)
    if normalized is None:
        msg = "pool config is required"
        raise ValueError(msg)
    if normalized.transport is not BackendRouteTransport.PrivateNetwork:
        msg = f"unsupported agent transport {normalized.transport.value!r}"
        raise ValueError(msg)
    if normalized.fallback not in set(PrivateUnitFallback):
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
    return ComputeUnitPlan(
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


_TTL_PATTERN = re.compile(r"(?P<amount>[1-9][0-9]*)(?P<unit>[smhd])")
