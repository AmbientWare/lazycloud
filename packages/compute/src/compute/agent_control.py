from __future__ import annotations

import hashlib
import ipaddress
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

from foundation.network import worker_network_prefix
from foundation.shell import shell_quote
from pydantic import Field, field_validator
from shared.capacity import CAPACITY_OWNER_ID_PATTERN
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputeMachineEnrollmentStatus,
    ComputePreflightCheck,
    MachineReadinessPhase,
)
from shared.compute_policy import (
    MachinePool,
)

if TYPE_CHECKING:
    from database.repositories.compute import ComputeMachineEnrollmentRecord
from shared.contracts import ContractModel
from shared.gpu import GPU_ANY, normalize_gpu_type
from shared.routing import (
    AgentBackendRoute,
    BackendRouteState,
    BackendRouteTransport,
    RoutePrewarmDecision,
)
from shared.timestamps import to_utc, utc_now
from shared.usage import UsageBillingOwner

from compute.projection import (
    PoolConfig,
    PrivateUnitFallback,
    PrivateUnitState,
    normalize_backend_route_transport,
    normalize_unit_config,
    parse_ttl_seconds,
)
from compute.state import (
    ComputeAgentTokenState,
    ComputeAgentWorkerSlotState,
    ComputeJoinTokenState,
)

DEFAULT_PRIVATE_JOIN_TTL_SECONDS = 30 * 60
DEFAULT_PRIVATE_EXECUTOR = "container"
AGENT_STREAM_REFRESH_SECONDS = 30.0
AGENT_STREAM_HEARTBEAT_SECONDS = 10.0
AGENT_STREAM_EVENT_COALESCE_SECONDS = 0.025
ROUTE_PREWARM_INTERVAL_SECONDS = 30.0
ROUTE_PREWARM_TIMEOUT_SECONDS = 3.0


class JoinTokenDecision(StrEnum):
    Accepted = "accepted"
    Missing = "missing"
    InvalidOrExpired = "invalid-or-expired"
    FingerprintConflict = "fingerprint-conflict"
    PoolNotFound = "pool-not-found"
    OwnerMismatch = "owner-mismatch"


class PoolGpuDecision(StrEnum):
    Accepted = "accepted"
    LockedPoolGpu = "locked-pool-gpu"
    Rejected = "rejected"


class TransportCredentialDecision(StrEnum):
    Ready = "ready"
    Disabled = "disabled"
    Unsupported = "unsupported"


class AgentStreamDecision(StrEnum):
    SendSnapshot = "send-snapshot"
    InvalidAgentToken = "invalid-agent-token"
    TokenChanged = "token-changed"


class WorkerSlotDecision(StrEnum):
    Ensure = "ensure"
    PruneOnly = "prune-only"
    TokenRequired = "token-required"


class WorkerTokenKind(StrEnum):
    WorkerPrivate = "worker-private"
    Worker = "worker"


class WorkerStatus(StrEnum):
    Pending = "pending"
    Available = "available"
    Disabled = "disabled"


class ComputePrincipal(ContractModel):
    workspace_id: str
    owner_token_id: str


class JoinTokenCreationPlan(ContractModel):
    token: str
    token_hash: str
    ttl_seconds: int
    expires_at: datetime
    state: ComputeJoinTokenState


class JoinTokenBindingPlan(ContractModel):
    decision: JoinTokenDecision
    accepted: bool
    state: ComputeJoinTokenState | None = None
    should_save: bool = False
    ttl_seconds: int = 0
    err_msg: str = ""


class AgentJoinRequest(ContractModel):
    machine_fingerprint: str = ""
    hostname: str = ""
    os: str = ""
    arch: str = ""
    cpu_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    gpu: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    gpu_count: int = 0
    executor: str = ""
    schedulable: bool = True
    preflight: list[ComputePreflightCheck] = Field(default_factory=list)

    @field_validator(
        "cpu_count",
        "cpu_millicores",
        "memory_mb",
        "gpu_count",
    )
    @classmethod
    def counts_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "agent join resource counts cannot be negative"
            raise ValueError(msg)
        return value


class PoolGpuEnforcementPlan(ContractModel):
    decision: PoolGpuDecision
    accepted: bool
    machine_gpu: str = ""
    configured_gpu: str = ""
    pool_config_update: PoolConfig | None = None
    err_msg: str = ""


class AgentJoinPlan(ContractModel):
    decision: JoinTokenDecision
    accepted: bool
    err_msg: str = ""
    machine_id: str = ""
    agent_token: str = ""
    agent_state: ComputeAgentTokenState | None = None
    binding: JoinTokenBindingPlan | None = None
    gpu: PoolGpuEnforcementPlan | None = None
    should_save_agent: bool = False
    should_save_join_token: bool = False
    should_save_pool: bool = False
    should_register_pool: bool = False
    pool_config_update: PoolConfig | None = None


class GatewayEndpointConfig(ContractModel):
    http_url: str
    grpc_host: str = ""
    grpc_port: int = 443
    grpc_tls: bool = True

    @field_validator("grpc_port")
    @classmethod
    def grpc_port_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "gateway grpc port must be positive"
            raise ValueError(msg)
        return value


class AgentImageConfig(ContractModel):
    registry_store: str = ""
    clip_version: int = 2
    local_cache_enabled: bool = True


class TailnetConfig(ContractModel):
    control_url: str = ""


class TransportValidationPlan(ContractModel):
    decision: TransportCredentialDecision
    accepted: bool
    transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted
    err_msg: str = ""


class AgentBootstrapConfig(ContractModel):
    gateway_public_http_url: str
    gateway_runtime_http_url: str
    gateway_grpc_host: str = ""
    gateway_grpc_port: int = 443
    gateway_grpc_tls: bool = True
    workspace_id: str
    pool: MachinePool
    transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted
    executor: str = DEFAULT_PRIVATE_EXECUTOR
    fallback: PrivateUnitFallback = PrivateUnitFallback.Internal
    image_registry_store: str = ""
    image_clip_version: int = 2
    image_local_cache_enabled: bool = True


class AgentStreamTimingPlan(ContractModel):
    refresh_seconds: float = AGENT_STREAM_REFRESH_SECONDS
    heartbeat_seconds: float = AGENT_STREAM_HEARTBEAT_SECONDS
    event_coalesce_seconds: float = AGENT_STREAM_EVENT_COALESCE_SECONDS


class AgentCurrentStatePlan(ContractModel):
    decision: AgentStreamDecision
    accepted: bool
    err_msg: str = ""
    state: ComputeAgentTokenState | None = None


class AgentHeartbeatTouchPlan(ContractModel):
    state: ComputeAgentTokenState | None = None
    should_save: bool = False
    reason: str = ""


class AgentRouteStatusRequest(ContractModel):
    route_id: str
    state: BackendRouteState | str | None = None
    proxy_target: str = ""
    error: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)


class AgentRouteStatusPlan(ContractModel):
    accepted: bool
    # The route is already gone, so the report and the record agree and there is
    # nothing to write. Distinct from a refusal: the agent is telling the truth
    # about a route that outlived its container, which is ordinary, not an error.
    already_gone: bool = False
    err_msg: str = ""
    previous: AgentBackendRoute | None = None
    updated: AgentBackendRoute | None = None
    should_save: bool = False
    should_emit_event: bool = False
    should_prewarm: bool = False
    event_attrs: dict[str, str] = Field(default_factory=dict)


class RoutePrewarmAttemptPlan(ContractModel):
    decision: RoutePrewarmDecision
    should_attempt: bool
    proxy_target: str = ""
    next_attempts: dict[str, datetime] = Field(default_factory=dict)
    timeout_seconds: float = ROUTE_PREWARM_TIMEOUT_SECONDS


class TailnetPeerView(ContractModel):
    host_name: str = ""
    dns_name: str = ""
    tailnet_ips: list[str] = Field(default_factory=list)
    online: bool = False
    active: bool = False
    current_address: str = ""
    relay: str = ""
    last_handshake_at: datetime | None = None


class RoutePrewarmResultPlan(ContractModel):
    status: str
    message: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)


class AgentStreamSnapshotPlan(ContractModel):
    current: AgentCurrentStatePlan
    routes: list[AgentBackendRoute] = Field(default_factory=list)
    slots: list[ComputeAgentWorkerSlotState] = Field(default_factory=list)
    timing: AgentStreamTimingPlan | None = None


class WorkerRecord(ContractModel):
    id: str
    machine_id: str
    pool: MachinePool
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    status: WorkerStatus = WorkerStatus.Pending
    total_cpu: int = 0
    total_memory: int = 0
    gpu: str = ""
    total_gpu_count: int = 0


class WorkerTokenRecord(ContractModel):
    key: str
    external_id: str
    active: bool = True
    disabled_by_cluster_admin: bool = False
    token_type: WorkerTokenKind = WorkerTokenKind.WorkerPrivate
    worker_id: str = ""
    reusable: bool = False


class AgentWorkerTokenPlan(ContractModel):
    accepted: bool
    err_msg: str = ""
    worker_token: str = ""
    worker_token_id: str = ""
    worker_token_hash: str = ""
    reused_existing: bool = False
    should_create: bool = False


class AgentWorkerSlotControlPlan(ContractModel):
    decision: WorkerSlotDecision
    accepted: bool
    err_msg: str = ""
    slot: ComputeAgentWorkerSlotState | None = None
    worker_token: str = ""
    pruned_worker_ids: list[str] = Field(default_factory=list)
    created_slot: bool = False


def generate_compute_token(nbytes: int = 32) -> str:
    if nbytes <= 0:
        msg = "token byte length must be positive"
        raise ValueError(msg)
    return secrets.token_urlsafe(nbytes)


def hash_compute_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_machine_fingerprint(fingerprint: str) -> str:
    normalized = fingerprint.strip()
    if normalized == "":
        msg = "machine fingerprint is required"
        raise ValueError(msg)
    return hashlib.sha256(normalized.encode()).hexdigest()


def agent_machine_id(workspace_id: str, pool: str, fingerprint: str, *, seed: str = "") -> str:
    id_seed = fingerprint or seed or str(int(utc_now().timestamp() * 1_000_000_000))
    return str(uuid5(NAMESPACE_URL, f"agent-machine\x00{workspace_id}\x00{pool}\x00{id_seed}"))


def agent_machine_worker_id(machine_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"agent-worker\x00{machine_id}"))


class MachineWorkerState(Protocol):
    """Narrow view of the scheduler's hot worker state.

    Implementations raise when the state store is unreachable rather than
    answering False: the reclaim path terminates billable machines on this
    answer, and an outage must read as "unknown", never as "gone".
    """

    def machine_worker_available(self, machine_id: str) -> bool: ...


def machine_serves_workloads(
    enrollment: ComputeMachineEnrollmentRecord | None,
    *,
    machine_id: str,
    worker_state: MachineWorkerState,
) -> bool:
    """Whether this machine accepts workloads right now.

    Two facts with two owners, and both must hold: the durable enrollment says
    the agent is alive and ready, and the scheduler's hot record says the
    worker takes work. The API summary and bootstrap reclaim previously each
    composed their own version from the durable `Worker` row, which asserts
    `Running` at registration — before the worker can accept anything.
    """
    if enrollment is None:
        return False
    if enrollment.status is not ComputeMachineEnrollmentStatus.Active:
        return False
    if enrollment.readiness_phase is not MachineReadinessPhase.Ready:
        return False
    return worker_state.machine_worker_available(machine_id)


def join_token_ttl_seconds(value: str) -> int:
    ttl = parse_ttl_seconds(value)
    if ttl == 0:
        return DEFAULT_PRIVATE_JOIN_TTL_SECONDS
    if ttl <= 0:
        msg = "join token ttl must be positive"
        raise ValueError(msg)
    return ttl


def plan_join_token_creation(
    principal: ComputePrincipal,
    pool: MachinePool,
    *,
    capacity_owner_id: str,
    owner_user_id: str,
    ttl: str = "",
    token: str = "",
    machine_id: str = "",
    max_uses: int = 1,
    now: datetime | None = None,
) -> JoinTokenCreationPlan:
    normalized_pool = pool.strip()
    if normalized_pool == "":
        msg = "pool name is required"
        raise ValueError(msg)
    if capacity_owner_id.strip() == "":
        msg = "join token requires the issuing capacity owner"
        raise ValueError(msg)
    if owner_user_id.strip() == "":
        msg = "join token requires the account the machine will belong to"
        raise ValueError(msg)
    if principal.workspace_id == "" or principal.owner_token_id == "":
        msg = "missing workspace auth"
        raise ValueError(msg)
    if max_uses <= 0:
        msg = "join token max uses must be positive"
        raise ValueError(msg)
    current_time = _utc(now)
    ttl_seconds = join_token_ttl_seconds(ttl)
    raw_token = token or generate_compute_token()
    expires_at = current_time + timedelta(seconds=ttl_seconds)
    state = ComputeJoinTokenState(
        token_hash=hash_compute_token(raw_token),
        owner_user_id=owner_user_id.strip(),
        workspace_id=principal.workspace_id,
        capacity_owner_id=capacity_owner_id,
        pool=MachinePool(normalized_pool),
        machine_id=machine_id.strip(),
        created_by_token_id=principal.owner_token_id,
        max_uses=max_uses,
        created_at=current_time,
        expires_at=expires_at,
    )
    return JoinTokenCreationPlan(
        token=raw_token,
        token_hash=state.token_hash,
        ttl_seconds=ttl_seconds,
        expires_at=expires_at,
        state=state,
    )


def plan_join_token_binding(
    token_state: ComputeJoinTokenState | None,
    fingerprint: str,
    *,
    now: datetime | None = None,
) -> JoinTokenBindingPlan:
    if token_state is None:
        return JoinTokenBindingPlan(
            decision=JoinTokenDecision.InvalidOrExpired,
            accepted=False,
            err_msg="join token is invalid or expired",
        )
    if token_state.machine_id == "":
        return JoinTokenBindingPlan(
            decision=JoinTokenDecision.Accepted, accepted=True, state=token_state
        )
    normalized_fingerprint = fingerprint.strip()
    if normalized_fingerprint == "":
        return JoinTokenBindingPlan(
            decision=JoinTokenDecision.Accepted, accepted=True, state=token_state
        )
    if token_state.bound_fingerprint == "":
        current_time = _utc(now)
        expires_at = _utc(token_state.expires_at) if token_state.expires_at else current_time
        ttl_seconds = max(int((expires_at - current_time).total_seconds()), 1)
        return JoinTokenBindingPlan(
            decision=JoinTokenDecision.Accepted,
            accepted=True,
            state=token_state.model_copy(update={"bound_fingerprint": normalized_fingerprint}),
            should_save=True,
            ttl_seconds=ttl_seconds,
        )
    if token_state.bound_fingerprint != normalized_fingerprint:
        return JoinTokenBindingPlan(
            decision=JoinTokenDecision.FingerprintConflict,
            accepted=False,
            state=token_state,
            err_msg="join token is already bound to another machine",
        )
    return JoinTokenBindingPlan(
        decision=JoinTokenDecision.Accepted, accepted=True, state=token_state
    )


def plan_agent_join(
    token_state: ComputeJoinTokenState | None,
    pool_state: PrivateUnitState | None,
    request: AgentJoinRequest,
    *,
    agent_token: str = "",
    now: datetime | None = None,
    existing_machine_gpus: list[list[str]] | None = None,
    existing_agent: ComputeAgentTokenState | None = None,
    credential_id: str = "",
) -> AgentJoinPlan:
    current_time = _utc(now)
    token_error = _join_token_error(token_state, current_time)
    if token_error:
        return AgentJoinPlan(
            decision=JoinTokenDecision.InvalidOrExpired,
            accepted=False,
            err_msg=token_error,
        )
    if request.machine_fingerprint.strip() == "":
        return AgentJoinPlan(
            decision=JoinTokenDecision.Missing,
            accepted=False,
            err_msg="machine fingerprint is required",
        )
    binding = plan_join_token_binding(token_state, request.machine_fingerprint, now=current_time)
    if not binding.accepted:
        return AgentJoinPlan(
            decision=binding.decision,
            accepted=False,
            err_msg=binding.err_msg,
            binding=binding,
        )
    active_token = binding.state or token_state
    if pool_state is None:
        return AgentJoinPlan(
            decision=JoinTokenDecision.PoolNotFound,
            accepted=False,
            err_msg="pool not found",
            binding=binding,
        )
    # Compared by capacity owner, not by name: the credential carries the pool
    # its machine will join, and a pool is fed by several units, so its name
    # matches no single unit's.
    if (
        active_token is None
        or pool_state.workspace_id != active_token.workspace_id
        or pool_state.capacity_owner_id != active_token.capacity_owner_id
    ):
        return AgentJoinPlan(
            decision=JoinTokenDecision.OwnerMismatch,
            accepted=False,
            err_msg="join token is invalid or expired",
            binding=binding,
        )
    # A credential naming no account cannot stamp tenancy, and a machine whose
    # owner is empty serves no workspace at all. Refusing here keeps a host from
    # enrolling into capacity that can never be scheduled.
    if active_token.owner_user_id == "":
        return AgentJoinPlan(
            decision=JoinTokenDecision.OwnerMismatch,
            accepted=False,
            err_msg="join token names no account",
            binding=binding,
        )

    machine_id = (
        existing_agent.machine_id
        if existing_agent is not None
        else active_token.machine_id.strip()
        or agent_machine_id(
            active_token.workspace_id,
            active_token.pool,
            request.machine_fingerprint,
        )
    )
    gpu_plan = plan_pool_gpu_enforcement(
        pool_state,
        request.gpu,
        request.gpu_count,
        existing_machine_gpus=existing_machine_gpus or [],
    )
    if not gpu_plan.accepted:
        return AgentJoinPlan(
            decision=JoinTokenDecision.Accepted,
            accepted=False,
            err_msg=gpu_plan.err_msg,
            binding=binding,
            gpu=gpu_plan,
            machine_id=machine_id,
        )
    normalized_pool_config = normalize_unit_config(
        pool_state.config or PoolConfig(name=pool_state.name)
    )
    if normalized_pool_config is None:
        return AgentJoinPlan(
            decision=JoinTokenDecision.PoolNotFound,
            accepted=False,
            err_msg="pool config is required",
            binding=binding,
            machine_id=machine_id,
        )

    raw_agent_token = agent_token or generate_compute_token()
    required_preflight_ok = bool(request.preflight) and all(
        check.ok or not check.required for check in request.preflight
    )
    capacity_valid = (request.cpu_millicores > 0 or request.cpu_count > 0) and request.memory_mb > 0
    preflight_passed = request.schedulable and required_preflight_ok and capacity_valid
    agent_state = ComputeAgentTokenState(
        token_hash=hash_compute_token(raw_agent_token),
        owner_user_id=active_token.owner_user_id,
        workspace_id=active_token.workspace_id,
        capacity_owner_id=active_token.capacity_owner_id,
        pool=active_token.pool,
        machine_id=machine_id,
        credential_id=credential_id,
        credential_generation=(
            existing_agent.credential_generation + 1 if existing_agent is not None else 1
        ),
        machine_fingerprint=request.machine_fingerprint,
        hostname=request.hostname,
        os=request.os,
        arch=request.arch,
        cpu_count=request.cpu_count,
        cpu_millicores=request.cpu_millicores or request.cpu_count * 1000,
        memory_mb=request.memory_mb,
        gpus=request.gpu,
        gpu_ids=request.gpu_ids,
        gpu_count=request.gpu_count,
        executor=request.executor or DEFAULT_PRIVATE_EXECUTOR,
        preflight_passed=preflight_passed,
        heartbeat_confirmed=False,
        schedulable=False,
        capacity_state=(
            existing_agent.capacity_state
            if existing_agent is not None
            else AgentCapacityState.Available
        ),
        capacity_reason=existing_agent.capacity_reason if existing_agent is not None else "",
        capacity_observed_at=(
            existing_agent.capacity_observed_at if existing_agent is not None else None
        ),
        capacity_notice_at=(
            existing_agent.capacity_notice_at if existing_agent is not None else None
        ),
        preflight=request.preflight,
        created_at=existing_agent.created_at if existing_agent is not None else current_time,
        last_join_at=current_time,
        last_heartbeat_at=None,
        metadata={
            "pool_transport": normalized_pool_config.transport.value,
            "pool_mode": normalized_pool_config.mode.value,
            "pool_fallback": normalized_pool_config.fallback.value,
            "pool_source": str(getattr(pool_state.source, "value", pool_state.source)),
        },
    )
    return AgentJoinPlan(
        decision=JoinTokenDecision.Accepted,
        accepted=True,
        machine_id=machine_id,
        agent_token=raw_agent_token,
        agent_state=agent_state,
        binding=binding,
        gpu=gpu_plan,
        should_save_agent=True,
        should_save_join_token=binding.should_save,
        should_save_pool=gpu_plan.pool_config_update is not None,
        should_register_pool=True,
        pool_config_update=gpu_plan.pool_config_update,
    )


def plan_pool_gpu_enforcement(
    pool_state: PrivateUnitState,
    machine_gpus: list[str],
    machine_gpu_count: int,
    *,
    existing_machine_gpus: list[list[str]] | None = None,
) -> PoolGpuEnforcementPlan:
    try:
        machine_gpu = single_gpu_type(machine_gpus, machine_gpu_count)
    except ValueError as exc:
        return PoolGpuEnforcementPlan(
            decision=PoolGpuDecision.Rejected,
            accepted=False,
            err_msg=str(exc),
        )
    pool_gpu = configured_pool_gpu(pool_state.config)
    if pool_gpu:
        if machine_gpu == "":
            return PoolGpuEnforcementPlan(
                decision=PoolGpuDecision.Rejected,
                accepted=False,
                configured_gpu=pool_gpu,
                err_msg=(
                    f"pool {pool_state.name!r} requires GPU type {pool_gpu!r}, "
                    "but machine has no GPUs"
                ),
            )
        if machine_gpu != pool_gpu:
            return PoolGpuEnforcementPlan(
                decision=PoolGpuDecision.Rejected,
                accepted=False,
                machine_gpu=machine_gpu,
                configured_gpu=pool_gpu,
                err_msg=(
                    f"pool {pool_state.name!r} requires GPU type {pool_gpu!r}, "
                    f"but machine reported {machine_gpu!r}"
                ),
            )
        return PoolGpuEnforcementPlan(
            decision=PoolGpuDecision.Accepted,
            accepted=True,
            machine_gpu=machine_gpu,
            configured_gpu=pool_gpu,
        )

    try:
        existing_gpu = existing_pool_gpu_type(existing_machine_gpus or [])
    except ValueError as exc:
        return PoolGpuEnforcementPlan(
            decision=PoolGpuDecision.Rejected,
            accepted=False,
            err_msg=str(exc),
        )
    has_existing = bool(existing_machine_gpus)
    if has_existing:
        if existing_gpu == "":
            if machine_gpu:
                return PoolGpuEnforcementPlan(
                    decision=PoolGpuDecision.Rejected,
                    accepted=False,
                    machine_gpu=machine_gpu,
                    err_msg=(
                        f"pool {pool_state.name!r} was initialized without GPUs; "
                        f"create a separate pool for GPU type {machine_gpu!r}"
                    ),
                )
            return PoolGpuEnforcementPlan(decision=PoolGpuDecision.Accepted, accepted=True)
        if machine_gpu == "":
            return PoolGpuEnforcementPlan(
                decision=PoolGpuDecision.Rejected,
                accepted=False,
                configured_gpu=existing_gpu,
                err_msg=f"pool {pool_state.name!r} requires GPU type {existing_gpu!r}",
            )
        if machine_gpu != existing_gpu:
            return PoolGpuEnforcementPlan(
                decision=PoolGpuDecision.Rejected,
                accepted=False,
                machine_gpu=machine_gpu,
                configured_gpu=existing_gpu,
                err_msg=(
                    f"pool {pool_state.name!r} requires GPU type {existing_gpu!r}, "
                    f"but machine reported {machine_gpu!r}"
                ),
            )
        return PoolGpuEnforcementPlan(
            decision=PoolGpuDecision.LockedPoolGpu,
            accepted=True,
            machine_gpu=machine_gpu,
            configured_gpu=existing_gpu,
            pool_config_update=_pool_config_with_gpu(pool_state, existing_gpu),
        )

    if machine_gpu == "":
        return PoolGpuEnforcementPlan(decision=PoolGpuDecision.Accepted, accepted=True)
    return PoolGpuEnforcementPlan(
        decision=PoolGpuDecision.LockedPoolGpu,
        accepted=True,
        machine_gpu=machine_gpu,
        configured_gpu=machine_gpu,
        pool_config_update=_pool_config_with_gpu(pool_state, machine_gpu),
    )


def single_gpu_type(gpus: list[str], gpu_count: int) -> str:
    seen: list[str] = []
    selected = ""
    for gpu in gpus:
        current = normalize_gpu_type(gpu)
        if current in {"", GPU_ANY}:
            continue
        if current not in seen:
            seen.append(current)
        if selected == "":
            selected = current
            continue
        if current != selected:
            msg = f"machine has mixed GPU types {', '.join(seen)}"
            raise ValueError(msg)
    if gpu_count > 0 and selected == "":
        msg = "machine reported GPU capacity without a GPU type"
        raise ValueError(msg)
    return selected


def configured_pool_gpu(config: PoolConfig | None) -> str:
    if config is None:
        return ""
    for gpu in config.gpu:
        normalized = normalize_gpu_type(gpu)
        if normalized and normalized != GPU_ANY:
            return normalized
    return ""


def existing_pool_gpu_type(machine_gpus: list[list[str]]) -> str:
    selected = ""
    for gpus in machine_gpus:
        machine_gpu = single_gpu_type(gpus, 0)
        if machine_gpu == "":
            continue
        if selected == "":
            selected = machine_gpu
            continue
        if selected != machine_gpu:
            msg = f"pool already has mixed GPU types {selected} and {machine_gpu}"
            raise ValueError(msg)
    return selected


def is_local_gateway_url(raw_url: str) -> bool:
    parsed = urlparse(raw_url)
    host = (parsed.hostname or raw_url).strip()
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def agent_install_command(
    gateway_url: str,
    token: str,
    *,
    agent_version: str = "",
    agent_sha256_by_arch: Mapping[str, str] | None = None,
    dev_mode: bool | None = None,
) -> str:
    normalized_gateway = gateway_url.rstrip("/")
    local_dev = is_local_gateway_url(normalized_gateway) if dev_mode is None else dev_mode
    install_url = shell_quote(f"{normalized_gateway}/install/agent")
    args = f"--gateway {shell_quote(normalized_gateway)} --join-token {shell_quote(token)}"
    if local_dev:
        return f"curl -fsSL {install_url} | sh -s -- {args} --dev"
    version = agent_version.strip()
    digests = agent_sha256_by_arch or {}
    amd64_sha256 = digests.get("amd64", "").strip()
    arm64_sha256 = digests.get("arm64", "").strip()
    if not version or (not amd64_sha256 and not arm64_sha256):
        msg = "immutable agent artifact version and architecture digests are required"
        raise ValueError(msg)
    artifact_args = f" --agent-version {shell_quote(version)}"
    if amd64_sha256:
        artifact_args += f" --agent-amd64-sha256 {shell_quote(amd64_sha256)}"
    if arm64_sha256:
        artifact_args += f" --agent-arm64-sha256 {shell_quote(arm64_sha256)}"
    args += artifact_args
    return (
        'if [ "$(uname -s)" = "Darwin" ] || [ "$(id -u)" -eq 0 ]; '
        f"then curl -fsSL {install_url} | sh -s -- {args}; "
        f"else curl -fsSL {install_url} | sudo sh -s -- {args}; fi"
    )


def validate_agent_transport_config(
    transport: BackendRouteTransport | str,
    tailnet: TailnetConfig,
) -> TransportValidationPlan:
    try:
        normalized = normalize_backend_route_transport(str(transport))
    except ValueError:
        return TransportValidationPlan(
            decision=TransportCredentialDecision.Unsupported,
            accepted=False,
            err_msg=f"unsupported agent transport {transport!r}",
        )
    if normalized is not BackendRouteTransport.TsnetRestricted:
        return TransportValidationPlan(
            decision=TransportCredentialDecision.Unsupported,
            accepted=False,
            transport=normalized,
            err_msg=f"unsupported agent transport {normalized.value!r}",
        )
    return TransportValidationPlan(
        decision=TransportCredentialDecision.Ready,
        accepted=True,
        transport=normalized,
    )


_LOCAL_RUNTIME_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
# Tailscale addresses a remote machine genuinely reaches. IPv4 uses the CGNAT
# range, which `ipaddress` already reports as non-private; IPv6 uses a ULA
# prefix, which it reports as private, so the prefix is named here rather than
# leaving a working tailnet configuration to be refused as unroutable.
_TAILNET_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def _is_tailnet_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in network for network in _TAILNET_NETWORKS)


def host_is_unreachable_from_a_remote_machine(host: str) -> bool:
    """Whether a remote machine could never reach this host.

    A literal address is classified rather than pattern-matched: `10.0.0.150`
    carries dots and is not loopback, so a name-shaped check accepts a LAN
    address that resolves only on the control plane's own network. A name is
    accepted here and left to DNS, except a single label, which resolves only
    inside a container network.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    if _is_tailnet_address(address):
        return False
    return (
        address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified
    )


def _reject_unroutable_runtime_url(
    url: str,
    *,
    pool: MachinePool,
    transport: BackendRouteTransport,
) -> None:
    """Refuse a runtime callback a remote machine could never resolve.

    Workers validate readiness by calling this origin. A Compose service name or
    loopback address resolves on the control-plane host and nowhere else, so a
    remote machine enrolls, reports healthy, and then crash-loops its worker
    forever on `Name or service not known`. Failing here names the cause instead
    of producing a machine that looks ready and can never run work.
    """
    if transport is not BackendRouteTransport.TsnetRestricted:
        return
    host = urlparse(url).hostname or ""
    if not host:
        raise ValueError("remote-machine runtime callback URL has no host")
    if host in _LOCAL_RUNTIME_HOSTS or host_is_unreachable_from_a_remote_machine(host):
        raise ValueError(
            f"pool {pool!r} serves remote machines and cannot use runtime callback host "
            f"{host!r}: a remote machine cannot resolve it. Set "
            "LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL to a publicly reachable origin."
        )


def build_agent_bootstrap_config(
    workspace_id: str,
    pool_state: PrivateUnitState,
    gateway: GatewayEndpointConfig,
    image: AgentImageConfig,
    *,
    gateway_runtime_http_url: str,
    tailnet: TailnetConfig,
    executor: str = DEFAULT_PRIVATE_EXECUTOR,
) -> AgentBootstrapConfig:
    normalized = normalize_unit_config(pool_state.config or PoolConfig(name=pool_state.name))
    if normalized is None:
        msg = "pool config is required"
        raise ValueError(msg)
    transport_plan = validate_agent_transport_config(normalized.transport, tailnet)
    if not transport_plan.accepted:
        raise ValueError(transport_plan.err_msg)
    _reject_unroutable_runtime_url(
        gateway_runtime_http_url,
        pool=pool_state.pool,
        transport=normalized.transport,
    )
    return AgentBootstrapConfig(
        gateway_public_http_url=gateway.http_url,
        gateway_runtime_http_url=gateway_runtime_http_url,
        gateway_grpc_host=gateway.grpc_host,
        gateway_grpc_port=gateway.grpc_port,
        gateway_grpc_tls=gateway.grpc_tls,
        workspace_id=workspace_id,
        pool=pool_state.pool,
        transport=normalized.transport,
        executor=executor,
        fallback=normalized.fallback,
        image_registry_store=image.registry_store,
        image_clip_version=image.clip_version,
        image_local_cache_enabled=image.local_cache_enabled,
    )


def agent_stream_timing() -> AgentStreamTimingPlan:
    return AgentStreamTimingPlan()


def validate_current_agent_state(
    provided: ComputeAgentTokenState | None,
    current: ComputeAgentTokenState | None,
) -> AgentCurrentStatePlan:
    if provided is None or current is None:
        return AgentCurrentStatePlan(
            decision=AgentStreamDecision.InvalidAgentToken,
            accepted=False,
            err_msg="agent token is no longer current",
        )
    if provided.token_hash != current.token_hash:
        return AgentCurrentStatePlan(
            decision=AgentStreamDecision.TokenChanged,
            accepted=False,
            err_msg="agent token is no longer current",
        )
    return AgentCurrentStatePlan(
        decision=AgentStreamDecision.SendSnapshot,
        accepted=True,
        state=current,
    )


def plan_agent_heartbeat_touch(
    current: ComputeAgentTokenState | None,
    *,
    now: datetime | None = None,
) -> AgentHeartbeatTouchPlan:
    if current is None:
        return AgentHeartbeatTouchPlan(reason="missing-current-agent")
    current_time = _utc(now)
    last_heartbeat = _utc(current.last_heartbeat_at) if current.last_heartbeat_at else None
    if last_heartbeat is not None and last_heartbeat > current_time:
        return AgentHeartbeatTouchPlan(state=current, reason="heartbeat-is-newer-than-gateway")
    return AgentHeartbeatTouchPlan(
        state=current.model_copy(
            update={
                "last_heartbeat_at": current_time,
                "last_disconnect_at": None,
                "heartbeat_confirmed": True,
                "schedulable": (
                    current.preflight_passed
                    and current.capacity_state is AgentCapacityState.Available
                ),
            }
        ),
        should_save=True,
        reason="heartbeat-touched",
    )


def plan_agent_stream_snapshot(
    provided: ComputeAgentTokenState | None,
    current: ComputeAgentTokenState | None,
    routes: list[AgentBackendRoute],
    slots: list[ComputeAgentWorkerSlotState],
) -> AgentStreamSnapshotPlan:
    state_plan = validate_current_agent_state(provided, current)
    if not state_plan.accepted or state_plan.state is None:
        return AgentStreamSnapshotPlan(current=state_plan)
    return AgentStreamSnapshotPlan(
        current=state_plan,
        routes=agent_routes_for_stream(routes),
        slots=slots,
        timing=agent_stream_timing(),
    )


def agent_routes_for_stream(routes: list[AgentBackendRoute]) -> list[AgentBackendRoute]:
    return [route for route in routes if route.state is not BackendRouteState.Closing]


def plan_route_status_update(
    agent_state: ComputeAgentTokenState,
    route: AgentBackendRoute | None,
    request: AgentRouteStatusRequest,
    *,
    now: datetime | None = None,
) -> AgentRouteStatusPlan:
    if route is None:
        # A container exits, its route is deleted, and the agent reports on it a
        # moment later from the route set it was streamed. Refusing that made a
        # normal race fatal: the agent raised, exited, and every restart replayed
        # the same report. Ownership is not checked because there is no route to
        # check it against, and nothing is read or written in reply.
        return AgentRouteStatusPlan(accepted=True, already_gone=True)
    if (
        route.workspace_id != agent_state.workspace_id
        or route.pool != agent_state.pool
        or route.machine_id != agent_state.machine_id
    ):
        return AgentRouteStatusPlan(accepted=False, err_msg="route does not belong to this agent")

    previous_state = route.state
    previous_proxy_target = route.proxy_target
    previous_error = route.error
    update_state = _route_state(request.state) if request.state else previous_state
    updated = route.model_copy(
        update={
            "state": update_state,
            "proxy_target": request.proxy_target or route.proxy_target,
            "error": request.error,
            "updated_at": int(_utc(now).timestamp()),
        }
    )
    state_changed = previous_state is not updated.state
    proxy_changed = previous_proxy_target != updated.proxy_target
    error_changed = previous_error != updated.error
    should_emit = state_changed or proxy_changed or error_changed
    should_prewarm = updated.state is BackendRouteState.Ready and (
        previous_state is not BackendRouteState.Ready or proxy_changed
    )
    return AgentRouteStatusPlan(
        accepted=True,
        previous=route,
        updated=updated,
        should_save=True,
        should_emit_event=should_emit,
        should_prewarm=should_prewarm,
        event_attrs=route_status_event_attrs(updated, request.attrs),
    )


def route_status_event_attrs(
    route: AgentBackendRoute,
    request_attrs: dict[str, str] | None = None,
) -> dict[str, str]:
    attrs = {
        "kind": str(route.kind.value if isinstance(route.kind, StrEnum) else route.kind),
        "port": str(route.port),
        "protocol": str(
            route.protocol.value if isinstance(route.protocol, StrEnum) else route.protocol
        ),
    }
    for key, value in (request_attrs or {}).items():
        if key.strip() and value:
            attrs[key] = value
    return attrs


def plan_route_prewarm_attempt(
    route: AgentBackendRoute,
    attempts: dict[str, datetime] | None = None,
    *,
    now: datetime | None = None,
    interval_seconds: float = ROUTE_PREWARM_INTERVAL_SECONDS,
) -> RoutePrewarmAttemptPlan:
    current_time = _utc(now)
    proxy_target = route.proxy_target.strip()
    next_attempts = dict(attempts or {})
    if route.state is not BackendRouteState.Ready:
        return RoutePrewarmAttemptPlan(
            decision=RoutePrewarmDecision.NotReady,
            should_attempt=False,
            proxy_target=proxy_target,
            next_attempts=next_attempts,
        )
    try:
        transport = _transport(route.transport)
    except ValueError:
        transport = BackendRouteTransport.Direct
    if transport is not BackendRouteTransport.TsnetRestricted:
        return RoutePrewarmAttemptPlan(
            decision=RoutePrewarmDecision.UnsupportedTransport,
            should_attempt=False,
            proxy_target=proxy_target,
            next_attempts=next_attempts,
        )
    if proxy_target == "":
        return RoutePrewarmAttemptPlan(
            decision=RoutePrewarmDecision.EmptyTarget,
            should_attempt=False,
            next_attempts=next_attempts,
        )
    last_attempt = next_attempts.get(proxy_target)
    if last_attempt is not None and current_time - _utc(last_attempt) < timedelta(
        seconds=interval_seconds
    ):
        return RoutePrewarmAttemptPlan(
            decision=RoutePrewarmDecision.Throttled,
            should_attempt=False,
            proxy_target=proxy_target,
            next_attempts=next_attempts,
        )
    next_attempts[proxy_target] = current_time
    return RoutePrewarmAttemptPlan(
        decision=RoutePrewarmDecision.Attempt,
        should_attempt=True,
        proxy_target=proxy_target,
        next_attempts=next_attempts,
    )


def route_peer_attrs(
    proxy_target: str,
    peers: list[TailnetPeerView],
    *,
    now: datetime | None = None,
    status_error: str = "",
) -> dict[str, str]:
    if status_error:
        return {"peer_status_error": status_error}
    host = _proxy_target_host(proxy_target)
    if host == "":
        return {}
    current_time = _utc(now)
    for peer in peers:
        if not peer_matches_host(peer.host_name, peer.dns_name, host):
            continue
        attrs = {
            "peer_online": str(peer.online).lower(),
            "peer_active": str(peer.active).lower(),
            "peer_direct": str(bool(peer.current_address)).lower(),
        }
        if peer.relay:
            attrs["peer_relay"] = peer.relay
        if peer.tailnet_ips:
            attrs["peer_tailnet_ips"] = ",".join(peer.tailnet_ips)
        if peer.last_handshake_at is not None:
            age = max(current_time - _utc(peer.last_handshake_at), timedelta())
            attrs["peer_last_handshake_age_ms"] = str(int(age.total_seconds() * 1000))
        return attrs
    return {"peer_status": "not_found"}


def peer_matches_host(host_name: str, dns_name: str, target: str) -> bool:
    normalized_target = target.strip().rstrip(".")
    normalized_host = host_name.strip().rstrip(".")
    normalized_dns = dns_name.strip().rstrip(".")
    return normalized_target in (normalized_host, normalized_dns) or normalized_dns.startswith(
        f"{normalized_target}."
    )


def plan_route_prewarm_result(
    route: AgentBackendRoute,
    *,
    dial_latency_ms: int,
    error: str = "",
    peer_attrs: dict[str, str] | None = None,
) -> RoutePrewarmResultPlan:
    attrs = {
        "proxy_target": route.proxy_target,
        "dial_ms": str(max(dial_latency_ms, 0)),
    }
    attrs.update(peer_attrs or {})
    if error:
        attrs["reason"] = error
        return RoutePrewarmResultPlan(status="error", message=error, attrs=attrs)
    return RoutePrewarmResultPlan(status="ready", attrs=attrs)


def plan_agent_worker_token(
    existing_slot: ComputeAgentWorkerSlotState | None,
    *,
    expected_worker_id: str,
    existing_token: WorkerTokenRecord | None = None,
    created_token: WorkerTokenRecord | None = None,
) -> AgentWorkerTokenPlan:
    if existing_slot is not None and existing_slot.worker_token_id and existing_token is not None:
        token_hash = hash_compute_token(existing_token.key)
        reusable = (
            existing_token.active
            and not existing_token.disabled_by_cluster_admin
            and existing_token.token_type is WorkerTokenKind.WorkerPrivate
            and existing_token.reusable
            and existing_token.worker_id == expected_worker_id
            and existing_slot.worker_id == expected_worker_id
            and existing_token.external_id == existing_slot.worker_token_id
            and (
                existing_slot.worker_token_hash == ""
                or existing_slot.worker_token_hash == token_hash
            )
        )
        if reusable:
            return AgentWorkerTokenPlan(
                accepted=True,
                worker_token=existing_token.key,
                worker_token_id=existing_token.external_id,
                worker_token_hash=token_hash,
                reused_existing=True,
            )
    if created_token is None:
        return AgentWorkerTokenPlan(
            accepted=False,
            err_msg="worker token creation is required",
            should_create=True,
        )
    if (
        not created_token.active
        or created_token.disabled_by_cluster_admin
        or created_token.token_type is not WorkerTokenKind.WorkerPrivate
        or not created_token.reusable
        or created_token.worker_id != expected_worker_id
    ):
        return AgentWorkerTokenPlan(
            accepted=False,
            err_msg="created worker token does not match the worker slot",
        )
    return AgentWorkerTokenPlan(
        accepted=True,
        worker_token=created_token.key,
        worker_token_id=created_token.external_id,
        worker_token_hash=hash_compute_token(created_token.key),
        should_create=True,
    )


def plan_agent_worker_slot(
    agent_state: ComputeAgentTokenState,
    worker: WorkerRecord | None,
    existing_slots: list[ComputeAgentWorkerSlotState],
    token_plan: AgentWorkerTokenPlan | None,
    *,
    billing_owner: UsageBillingOwner,
    cluster_name: str,
    worker_image: str,
) -> AgentWorkerSlotControlPlan:
    if (
        worker is None
        or worker.machine_id != agent_state.machine_id
        or worker.pool != agent_state.pool
        or worker.status is WorkerStatus.Disabled
    ):
        return AgentWorkerSlotControlPlan(
            decision=WorkerSlotDecision.PruneOnly,
            accepted=True,
            pruned_worker_ids=[slot.worker_id for slot in existing_slots if slot.worker_id],
        )
    existing = next((slot for slot in existing_slots if slot.worker_id == worker.id), None)
    if token_plan is None or not token_plan.accepted:
        return AgentWorkerSlotControlPlan(
            decision=WorkerSlotDecision.TokenRequired,
            accepted=False,
            err_msg=(token_plan.err_msg if token_plan is not None else "worker token is required"),
            pruned_worker_ids=[
                slot.worker_id
                for slot in existing_slots
                if slot.worker_id and slot.worker_id != worker.id
            ],
        )
    slot = agent_worker_slot_state(
        agent_state,
        worker,
        token_plan.worker_token_id,
        token_plan.worker_token_hash,
        billing_owner=billing_owner,
        cluster_name=cluster_name,
        worker_image=worker_image,
        existing=existing,
    )
    return AgentWorkerSlotControlPlan(
        decision=WorkerSlotDecision.Ensure,
        accepted=True,
        slot=slot,
        worker_token=token_plan.worker_token,
        pruned_worker_ids=[
            existing_slot.worker_id
            for existing_slot in existing_slots
            if existing_slot.worker_id and existing_slot.worker_id != worker.id
        ],
        created_slot=existing is None,
    )


def agent_worker_slot_state(
    agent_state: ComputeAgentTokenState,
    worker: WorkerRecord,
    token_id: str,
    token_hash: str,
    *,
    billing_owner: UsageBillingOwner,
    cluster_name: str,
    worker_image: str,
    existing: ComputeAgentWorkerSlotState | None = None,
) -> ComputeAgentWorkerSlotState:
    return ComputeAgentWorkerSlotState(
        worker_id=worker.id,
        worker_token_id=token_id,
        worker_token_hash=token_hash,
        workspace_id=agent_state.workspace_id,
        pool=agent_state.pool,
        capacity_owner_id=worker.capacity_owner_id,
        machine_id=agent_state.machine_id,
        cpu=worker.total_cpu,
        memory=worker.total_memory,
        gpu=worker.gpu,
        gpu_count=worker.total_gpu_count,
        gpu_assignment=",".join(agent_state.gpu_ids),
        billing_owner=billing_owner,
        network_prefix=worker_network_prefix(cluster_name, agent_state.machine_id),
        worker_image=worker_image,
        created_at=existing.created_at if existing else utc_now(),
    )


def agent_worker_image(registry: str, name: str, tag: str = "") -> str:
    image = registry.rstrip("/")
    if image:
        image += "/"
    image += name
    if tag:
        image += f":{tag}"
    return image


def _join_token_error(token_state: ComputeJoinTokenState | None, now: datetime) -> str:
    if token_state is None or token_state.revoked or token_state.expires_at is None:
        return "join token is invalid or expired"
    if now > _utc(token_state.expires_at):
        return "join token is invalid or expired"
    return ""


def _pool_config_with_gpu(pool_state: PrivateUnitState, gpu: str) -> PoolConfig:
    base = pool_state.config or PoolConfig(name=pool_state.name, selector=pool_state.selector)
    return base.model_copy(update={"gpu": [gpu]})


def _route_state(value: BackendRouteState | str) -> BackendRouteState:
    if isinstance(value, BackendRouteState):
        return value
    return BackendRouteState(str(value))


def _transport(value: BackendRouteTransport | str) -> BackendRouteTransport:
    if isinstance(value, BackendRouteTransport):
        return value
    return normalize_backend_route_transport(str(value))


def _proxy_target_host(proxy_target: str) -> str:
    target = proxy_target.strip()
    if target == "":
        return ""
    if target.startswith("[") and "]" in target:
        return target[1 : target.index("]")].strip().rstrip(".")
    if ":" in target:
        host, maybe_port = target.rsplit(":", 1)
        if maybe_port.isdigit():
            target = host
    return target.strip().rstrip(".")


def _utc(value: datetime | None) -> datetime:
    return utc_now() if value is None else to_utc(value)
