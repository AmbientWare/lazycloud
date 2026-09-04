from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from coordination.redis_client import AsyncRedisClient, RedisClient
from coordination.redis_serialization import (
    dump_model_json,
    load_model_json,
    redis_strings,
    redis_text,
)
from pydantic import Field, JsonValue, field_validator
from shared.capacity import CAPACITY_OWNER_ID_PATTERN
from shared.compute_enrollment import (
    AgentCapacityState,
    AgentWorkerSlotStatus,
    ComputePreflightCheck,
)
from shared.compute_policy import MachinePool, UnitName
from shared.contracts import ContractModel
from shared.routing import AgentBackendRoute
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner

DEFAULT_COMPUTE_POOL_LOCK_TTL_SECONDS = 300
DEFAULT_COMPUTE_POOL_LOCK_RETRIES = 100
DEFAULT_COMPUTE_POOL_LOCK_RETRY_INTERVAL_MS = 100
DEFAULT_COMPUTE_JOIN_TOKEN_TTL_SECONDS = 60
DEFAULT_COMPUTE_AGENT_TOKEN_TTL_SECONDS = 86_400


class ComputeUnitStatus(StrEnum):
    Pending = "pending"
    Active = "active"
    Draining = "draining"
    Deleted = "deleted"


class ComputeUnitState(ContractModel):
    workspace_id: str
    name: UnitName
    """Unit this hot state belongs to. Pool state is per unit, never per pool."""
    pool: MachinePool = MachinePool("")
    """Pool the unit's machines serve. Carried so a consumer holding only this
    state never has to substitute the unit's name for the label."""
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    platform_fleet: bool = False
    """Whether this unit is the platform's own capacity, so the scheduler can
    register a machine's worker with the tenancy the unit decided."""
    default_eligible: bool = False
    """Whether this unit answers a workload that named no pool.

    Carried so the scheduler can register a machine's worker with the unit's
    policy instead of guessing one the unit alone knows."""
    provider: str = "agent"
    status: ComputeUnitStatus = ComputeUnitStatus.Active
    min_machines: int = 0
    max_machines: int = 1
    desired_machines: int = 0
    active_machines: int = 0
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("min_machines", "max_machines", "desired_machines", "active_machines")
    @classmethod
    def pool_counts_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "compute pool counts cannot be negative"
            raise ValueError(msg)
        return value


class ComputeJoinTokenState(ContractModel):
    token_hash: str
    owner_user_id: str = ""
    """Account the machine will belong to, and the whole of the private-placement rule.

    Carried from the durable credential so the join stamps tenancy from the authority
    that decided it, never from what the joining machine claims about itself. Empty is
    a credential that named no account: the join is refused rather than admitted to
    capacity nothing owns.
    """
    workspace_id: str
    capacity_owner_id: str = Field(min_length=1)
    """Unit that issued the credential, carried onto the machine that joins."""
    pool: MachinePool
    credential_id: str = ""
    machine_id: str = ""
    created_by_token_id: str = ""
    max_uses: int = 1
    use_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    revoked: bool = False
    bound_fingerprint: str = ""
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("max_uses", "use_count")
    @classmethod
    def token_counts_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "compute join token counts cannot be negative"
            raise ValueError(msg)
        return value


class ComputeAgentTokenState(ContractModel):
    token_hash: str
    owner_user_id: str = ""
    """Account that owns this machine; every workspace it holds may place here.

    Re-stamped from the durable enrollment whenever the agent is read, so a record
    written before the machine had an account converges instead of serving nothing
    forever.
    """
    workspace_id: str
    capacity_owner_id: str = Field(min_length=1)
    """Unit that bought this machine.

    Taken from the join credential rather than from the pool config: a joined
    machine in a pool an auto-scaling unit also feeds must never be selected by
    that unit's drain.
    """
    pool: MachinePool
    machine_id: str
    credential_id: str = ""
    credential_generation: int = 1
    agent_id: str = ""
    machine_fingerprint: str = ""
    hostname: str = ""
    os: str = ""
    arch: str = ""
    cpu_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    gpus: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    gpu_count: int = 0
    executor: str = ""
    preflight_passed: bool = False
    heartbeat_confirmed: bool = False
    schedulable: bool = False
    capacity_state: AgentCapacityState = AgentCapacityState.Available
    capacity_reason: str = ""
    capacity_observed_at: datetime | None = None
    capacity_notice_at: datetime | None = None
    preflight: list[ComputePreflightCheck] = Field(default_factory=list)
    agent_version: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    last_join_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("credential_generation")
    @classmethod
    def credential_generation_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "agent credential generation must be positive"
            raise ValueError(msg)
        return value


class ComputeAgentWorkerSlotState(ContractModel):
    workspace_id: str
    pool: MachinePool
    machine_id: str
    worker_id: str
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    billing_owner: UsageBillingOwner
    """How this machine's containers price.

    No default: the slot is the only carrier that reaches a worker the platform
    did not start itself, and a slot that guessed would bill a customer's own
    hardware as capacity we sold them.
    """

    status: AgentWorkerSlotStatus = AgentWorkerSlotStatus.Active
    container_id: str = ""
    worker_token_id: str = ""
    worker_token_hash: str = ""
    cpu: int = 0
    memory: int = 0
    gpu: str = ""
    gpu_count: int = 0
    gpu_assignment: str = ""
    network_prefix: str = ""
    worker_image: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ComputeUnitLockPlan(ContractModel):
    workspace_id: str
    capacity_owner_id: str
    key: str
    ttl_seconds: int = DEFAULT_COMPUTE_POOL_LOCK_TTL_SECONDS
    retries: int = DEFAULT_COMPUTE_POOL_LOCK_RETRIES
    retry_interval_ms: int = DEFAULT_COMPUTE_POOL_LOCK_RETRY_INTERVAL_MS


@dataclass(frozen=True, slots=True)
class ComputeStateKeys:
    redis: RedisClient | AsyncRedisClient
    namespace: str = "compute"

    def agent_route_pattern(self) -> str:
        return self.redis.key(
            self.namespace,
            "workspaces",
            "*",
            "units",
            "*",
            "machines",
            "*",
            "routes",
            "*",
        )

    def pool_state(self, workspace_id: str, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace, "workspaces", workspace_id, "units", capacity_owner_id
        )

    def pool_index(self, workspace_id: str) -> str:
        return self.redis.key(self.namespace, "workspaces", workspace_id, "unit-index")

    def pool_workspace_index(self) -> str:
        return self.redis.key(self.namespace, "workspace-index")

    def pool_state_lock(self, workspace_id: str, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace, "workspaces", workspace_id, "units", capacity_owner_id, "lock"
        )

    def join_token(self, token_hash: str) -> str:
        return self.redis.key(self.namespace, "join-tokens", token_hash)

    def join_token_index(self, workspace_id: str, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "workspaces",
            workspace_id,
            "units",
            capacity_owner_id,
            "join-token-index",
        )

    def agent_token(self, token_hash: str) -> str:
        return self.redis.key(self.namespace, "agent-tokens", token_hash)

    def agent_machine(self, workspace_id: str, capacity_owner_id: str, machine_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "workspaces",
            workspace_id,
            "units",
            capacity_owner_id,
            "machines",
            machine_id,
        )

    def agent_machine_owner(self, workspace_id: str, machine_id: str) -> str:
        return self.redis.key(
            self.namespace, "workspaces", workspace_id, "machines", machine_id, "unit"
        )

    def agent_machine_index(self, workspace_id: str, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "workspaces",
            workspace_id,
            "units",
            capacity_owner_id,
            "machine-index",
        )

    def agent_slot(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
        worker_id: str,
    ) -> str:
        return self.redis.key(
            self.namespace,
            "workspaces",
            workspace_id,
            "units",
            capacity_owner_id,
            "machines",
            machine_id,
            "slots",
            worker_id,
        )

    def agent_slot_index(self, workspace_id: str, capacity_owner_id: str, machine_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "workspaces",
            workspace_id,
            "units",
            capacity_owner_id,
            "machines",
            machine_id,
            "slot-index",
        )

    def agent_route(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
        route_id: str,
    ) -> str:
        return self.redis.key(
            self.namespace,
            "workspaces",
            workspace_id,
            "units",
            capacity_owner_id,
            "machines",
            machine_id,
            "routes",
            route_id,
        )

    def agent_route_index(self, workspace_id: str, capacity_owner_id: str, machine_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "workspaces",
            workspace_id,
            "units",
            capacity_owner_id,
            "machines",
            machine_id,
            "route-index",
        )

    def agent_route_revision(
        self, workspace_id: str, capacity_owner_id: str, machine_id: str
    ) -> str:
        return self.redis.key(
            self.namespace,
            "workspaces",
            workspace_id,
            "units",
            capacity_owner_id,
            "machines",
            machine_id,
            "route-revision",
        )


@dataclass(init=False, slots=True)
class RedisComputeStateRepository:
    redis: RedisClient
    keys: ComputeStateKeys

    def __init__(self, redis: RedisClient, keys: ComputeStateKeys | None = None) -> None:
        self.redis = redis
        self.keys = keys or ComputeStateKeys(redis)

    def pool_lock_plan(self, workspace_id: str, capacity_owner_id: str) -> ComputeUnitLockPlan:
        return ComputeUnitLockPlan(
            workspace_id=workspace_id,
            capacity_owner_id=capacity_owner_id,
            key=self.keys.pool_state_lock(workspace_id, capacity_owner_id),
        )

    def save_unit_state(self, state: ComputeUnitState) -> ComputeUnitState:
        self.redis.set(
            self.keys.pool_state(state.workspace_id, state.capacity_owner_id),
            dump_model_json(state),
        )
        self.redis.set_add(self.keys.pool_index(state.workspace_id), state.capacity_owner_id)
        self.redis.set_add(self.keys.pool_workspace_index(), state.workspace_id)
        return state

    def get_unit_state(self, workspace_id: str, capacity_owner_id: str) -> ComputeUnitState | None:
        raw = self.redis.get(self.keys.pool_state(workspace_id, capacity_owner_id))
        if raw is None:
            return None
        state = load_model_json(ComputeUnitState, raw)
        if state.workspace_id == "":
            return state.model_copy(update={"workspace_id": workspace_id})
        return state

    def list_pool_states(self, workspace_id: str, *, limit: int = 0) -> list[ComputeUnitState]:
        names = sorted(redis_strings(self.redis.set_members(self.keys.pool_index(workspace_id))))
        if limit > 0:
            names = names[:limit]
        return [
            state
            for name in names
            if (state := self.get_unit_state(workspace_id, name)) is not None
        ]

    def list_all_pool_states(self, *, limit: int = 0) -> list[ComputeUnitState]:
        states: list[ComputeUnitState] = []
        workspace_ids = sorted(
            redis_strings(self.redis.set_members(self.keys.pool_workspace_index()))
        )
        for workspace_id in workspace_ids:
            remaining = 0 if limit <= 0 else limit - len(states)
            if limit > 0 and remaining <= 0:
                break
            states.extend(self.list_pool_states(workspace_id, limit=remaining))
        return states

    def delete_unit_state(self, workspace_id: str, capacity_owner_id: str) -> bool:
        machine_index = self.keys.agent_machine_index(workspace_id, capacity_owner_id)
        for machine_id in redis_strings(self.redis.set_members(machine_index)):
            self.delete_agent_machine_state(workspace_id, capacity_owner_id, machine_id)
        pool_root = self.keys.pool_state(workspace_id, capacity_owner_id)
        owned_descendants = self.redis.scan(f"{pool_root}:*")
        join_token_index = self.keys.join_token_index(workspace_id, capacity_owner_id)
        join_token_hashes = redis_strings(self.redis.set_members(join_token_index))
        join_token_keys = [self.keys.join_token(token_hash) for token_hash in join_token_hashes]
        deleted = bool(
            self.redis.delete(
                self.keys.pool_state(workspace_id, capacity_owner_id),
                self.keys.pool_state_lock(workspace_id, capacity_owner_id),
                machine_index,
                join_token_index,
                *join_token_keys,
                *owned_descendants,
            )
        )
        self.redis.set_remove(self.keys.pool_index(workspace_id), capacity_owner_id)
        if self.redis.set_cardinality(self.keys.pool_index(workspace_id)) == 0:
            self.redis.set_remove(self.keys.pool_workspace_index(), workspace_id)
        return deleted

    def delete_workspace_state(self, workspace_id: str) -> int:
        workspace_root = self.redis.key(self.keys.namespace, "workspaces", workspace_id)
        owned_keys = self.redis.scan(f"{workspace_root}:*")
        owned_keys.extend(self._workspace_join_token_keys(workspace_id))
        owned_keys.extend(self._workspace_agent_token_keys(workspace_id))
        deleted = int(self.redis.delete(*owned_keys)) if owned_keys else 0
        self.redis.set_remove(self.keys.pool_workspace_index(), workspace_id)
        return deleted

    def _workspace_join_token_keys(self, workspace_id: str) -> list[str]:
        pattern = self.redis.key(self.keys.namespace, "join-tokens", "*")
        keys: list[str] = []
        for key in self.redis.scan(pattern):
            raw = self.redis.get(key)
            if raw is None:
                continue
            if load_model_json(ComputeJoinTokenState, raw).workspace_id == workspace_id:
                keys.append(key)
        return keys

    def _workspace_agent_token_keys(self, workspace_id: str) -> list[str]:
        pattern = self.redis.key(self.keys.namespace, "agent-tokens", "*")
        keys: list[str] = []
        for key in self.redis.scan(pattern):
            raw = self.redis.get(key)
            if raw is None:
                continue
            if load_model_json(ComputeAgentTokenState, raw).workspace_id == workspace_id:
                keys.append(key)
        return keys

    def save_join_token_state(
        self,
        state: ComputeJoinTokenState,
        *,
        ttl_seconds: int = DEFAULT_COMPUTE_JOIN_TOKEN_TTL_SECONDS,
    ) -> ComputeJoinTokenState:
        ttl = max(ttl_seconds, 1)
        self.redis.set(self.keys.join_token(state.token_hash), dump_model_json(state), ex=ttl)
        self.redis.set_add(
            self.keys.join_token_index(state.workspace_id, state.capacity_owner_id),
            state.token_hash,
        )
        return state

    def get_join_token_state(self, token_hash: str) -> ComputeJoinTokenState | None:
        raw = self.redis.get(self.keys.join_token(token_hash))
        if raw is None:
            return None
        return load_model_json(ComputeJoinTokenState, raw)

    def revoke_join_token_state(self, token_hash: str) -> bool:
        state = self.get_join_token_state(token_hash)
        if state is None:
            return False
        now = utc_now()
        ttl_seconds = (
            max(int((state.expires_at - now).total_seconds()), 1)
            if state.expires_at is not None
            else DEFAULT_COMPUTE_JOIN_TOKEN_TTL_SECONDS
        )
        self.save_join_token_state(
            state.model_copy(update={"revoked": True}), ttl_seconds=ttl_seconds
        )
        return True

    def save_agent_token_state(
        self,
        state: ComputeAgentTokenState,
        *,
        ttl_seconds: int = DEFAULT_COMPUTE_AGENT_TOKEN_TTL_SECONDS,
    ) -> ComputeAgentTokenState:
        payload = dump_model_json(state)
        self.redis.set(self.keys.agent_token(state.token_hash), payload, ex=ttl_seconds)
        self.redis.set(
            self.keys.agent_machine(state.workspace_id, state.capacity_owner_id, state.machine_id),
            payload,
        )
        self.redis.set(
            self.keys.agent_machine_owner(state.workspace_id, state.machine_id),
            state.capacity_owner_id,
        )
        self.redis.set_add(
            self.keys.agent_machine_index(state.workspace_id, state.capacity_owner_id),
            state.machine_id,
        )
        return state

    def get_agent_token_state(self, token_hash: str) -> ComputeAgentTokenState | None:
        if token_hash == "":
            return None
        raw = self.redis.get(self.keys.agent_token(token_hash))
        if raw is None:
            return None
        return load_model_json(ComputeAgentTokenState, raw)

    def delete_agent_token_state(self, token_hash: str) -> bool:
        if token_hash == "":
            return False
        return bool(self.redis.delete(self.keys.agent_token(token_hash)))

    def get_agent_machine_state(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
    ) -> ComputeAgentTokenState | None:
        raw = self.redis.get(self.keys.agent_machine(workspace_id, capacity_owner_id, machine_id))
        if raw is None:
            return None
        return load_model_json(ComputeAgentTokenState, raw)

    def get_agent_machine_state_for_workspace(
        self,
        workspace_id: str,
        machine_id: str,
    ) -> ComputeAgentTokenState | None:
        capacity_owner_id = self.redis.get(self.keys.agent_machine_owner(workspace_id, machine_id))
        if capacity_owner_id is not None and redis_text(capacity_owner_id):
            return self.get_agent_machine_state(
                workspace_id, redis_text(capacity_owner_id), machine_id
            )
        for pool in self.list_pool_states(workspace_id):
            state = self.get_agent_machine_state(workspace_id, pool.capacity_owner_id, machine_id)
            if state is not None:
                return state
        return None

    def list_agent_token_states(
        self, workspace_id: str, capacity_owner_id: str
    ) -> list[ComputeAgentTokenState]:
        machine_ids = sorted(
            redis_strings(
                self.redis.set_members(
                    self.keys.agent_machine_index(workspace_id, capacity_owner_id)
                )
            )
        )
        states = [
            state
            for machine_id in machine_ids
            if (state := self.get_agent_machine_state(workspace_id, capacity_owner_id, machine_id))
            is not None
        ]
        states.sort(key=lambda item: item.machine_id)
        return states

    def unit_owner_for_machine(self, workspace_id: str, machine_id: str) -> str | None:
        """Resolve which unit a joined machine enrolled under.

        The reverse index is the only authority: the machine's pool label names
        the pool it serves, which several units share.
        """
        raw = self.redis.get(self.keys.agent_machine_owner(workspace_id, machine_id))
        if raw is None or not redis_text(raw):
            return None
        return redis_text(raw)

    def delete_agent_machine_state_for_machine(self, workspace_id: str, machine_id: str) -> bool:
        capacity_owner_id = self.unit_owner_for_machine(workspace_id, machine_id)
        if capacity_owner_id is None:
            return False
        return self.delete_agent_machine_state(workspace_id, capacity_owner_id, machine_id)

    def delete_agent_machine_state(
        self, workspace_id: str, capacity_owner_id: str, machine_id: str
    ) -> bool:
        state = self.get_agent_machine_state(workspace_id, capacity_owner_id, machine_id)
        worker_ids = redis_strings(
            self.redis.set_members(
                self.keys.agent_slot_index(workspace_id, capacity_owner_id, machine_id)
            )
        )
        keys = [
            self.keys.agent_slot(workspace_id, capacity_owner_id, machine_id, worker_id)
            for worker_id in worker_ids
        ]
        keys.extend(
            [
                self.keys.agent_slot_index(workspace_id, capacity_owner_id, machine_id),
                self.keys.agent_machine(workspace_id, capacity_owner_id, machine_id),
                self.keys.agent_machine_owner(workspace_id, machine_id),
            ]
        )
        route_ids = redis_strings(
            self.redis.set_members(
                self.keys.agent_route_index(workspace_id, capacity_owner_id, machine_id)
            )
        )
        keys.extend(
            self.keys.agent_route(workspace_id, capacity_owner_id, machine_id, route_id)
            for route_id in route_ids
        )
        keys.extend(
            [
                self.keys.agent_route_index(workspace_id, capacity_owner_id, machine_id),
                self.keys.agent_route_revision(workspace_id, capacity_owner_id, machine_id),
            ]
        )
        if state is not None and state.token_hash:
            keys.append(self.keys.agent_token(state.token_hash))
        deleted = bool(self.redis.delete(*keys))
        self.redis.set_remove(
            self.keys.agent_machine_index(workspace_id, capacity_owner_id), machine_id
        )
        return deleted

    def prune_agent_machine_index(self, workspace_id: str, capacity_owner_id: str) -> int:
        removed = 0
        index_key = self.keys.agent_machine_index(workspace_id, capacity_owner_id)
        for machine_id in redis_strings(self.redis.set_members(index_key)):
            if self.redis.exists(
                self.keys.agent_machine(workspace_id, capacity_owner_id, machine_id)
            ):
                continue
            removed += int(self.redis.set_remove(index_key, machine_id))
        return removed

    def save_agent_route_state(self, state: AgentBackendRoute) -> AgentBackendRoute:
        self.redis.set(
            self.keys.agent_route(
                state.workspace_id,
                state.capacity_owner_id,
                state.machine_id,
                state.route_id,
            ),
            dump_model_json(state),
        )
        self.redis.set_add(
            self.keys.agent_route_index(
                state.workspace_id, state.capacity_owner_id, state.machine_id
            ),
            state.route_id,
        )
        self.bump_agent_route_revision(
            state.workspace_id, state.capacity_owner_id, state.machine_id
        )
        return state

    def get_agent_route_state(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
        route_id: str,
    ) -> AgentBackendRoute | None:
        raw = self.redis.get(
            self.keys.agent_route(workspace_id, capacity_owner_id, machine_id, route_id)
        )
        if raw is None:
            return None
        return load_model_json(AgentBackendRoute, raw)

    def list_agent_route_states(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
    ) -> list[AgentBackendRoute]:
        route_ids = sorted(
            redis_strings(
                self.redis.set_members(
                    self.keys.agent_route_index(workspace_id, capacity_owner_id, machine_id)
                )
            )
        )
        states = [
            state
            for route_id in route_ids
            if (
                state := self.get_agent_route_state(
                    workspace_id,
                    capacity_owner_id,
                    machine_id,
                    route_id,
                )
            )
            is not None
        ]
        states.sort(key=lambda item: item.route_id)
        return states

    def scan_agent_route_states(self) -> list[AgentBackendRoute]:
        raw_states = self.redis.mget(self.redis.scan(self.keys.agent_route_pattern()))
        states = [load_model_json(AgentBackendRoute, raw) for raw in raw_states if raw is not None]
        states.sort(key=_agent_route_order)
        return states

    def delete_agent_route_state(
        self,
        *,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
        route_id: str,
    ) -> bool:
        # Keyword-only because the owner and the pool are both plain strings, and
        # positionally they are adjacent and interchangeable. A caller passing the
        # pool here deleted a key that never existed, silently, for every route.
        deleted = bool(
            self.redis.delete(
                self.keys.agent_route(workspace_id, capacity_owner_id, machine_id, route_id)
            )
        )
        self.redis.set_remove(
            self.keys.agent_route_index(workspace_id, capacity_owner_id, machine_id), route_id
        )
        self.bump_agent_route_revision(workspace_id, capacity_owner_id, machine_id)
        return deleted

    def bump_agent_route_revision(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
    ) -> int:
        key = self.keys.agent_route_revision(workspace_id, capacity_owner_id, machine_id)
        return self.redis.increment(key)

    def save_agent_worker_slot_state(
        self,
        state: ComputeAgentWorkerSlotState,
        *,
        now: datetime | None = None,
    ) -> ComputeAgentWorkerSlotState:
        current_time = now or utc_now()
        updated = state.model_copy(
            update={
                "created_at": state.created_at or current_time,
                "updated_at": current_time,
            }
        )
        self.redis.set(
            self.keys.agent_slot(
                updated.workspace_id,
                updated.capacity_owner_id,
                updated.machine_id,
                updated.worker_id,
            ),
            dump_model_json(updated),
        )
        self.redis.set_add(
            self.keys.agent_slot_index(
                updated.workspace_id, updated.capacity_owner_id, updated.machine_id
            ),
            updated.worker_id,
        )
        return updated

    def list_agent_worker_slot_states(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
    ) -> list[ComputeAgentWorkerSlotState]:
        worker_ids = sorted(
            redis_strings(
                self.redis.set_members(
                    self.keys.agent_slot_index(workspace_id, capacity_owner_id, machine_id)
                )
            )
        )
        states: list[ComputeAgentWorkerSlotState] = []
        for worker_id in worker_ids:
            raw = self.redis.get(
                self.keys.agent_slot(workspace_id, capacity_owner_id, machine_id, worker_id)
            )
            if raw is None:
                continue
            states.append(load_model_json(ComputeAgentWorkerSlotState, raw))
        states.sort(key=lambda item: item.worker_id)
        return states

    def delete_agent_worker_slot_state(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
        worker_id: str,
    ) -> bool:
        deleted = bool(
            self.redis.delete(
                self.keys.agent_slot(workspace_id, capacity_owner_id, machine_id, worker_id)
            )
        )
        self.redis.set_remove(
            self.keys.agent_slot_index(workspace_id, capacity_owner_id, machine_id), worker_id
        )
        return deleted


def _agent_route_order(route: AgentBackendRoute) -> tuple[str, str, str, str]:
    return (route.workspace_id, route.pool, route.machine_id, route.route_id)


@dataclass(init=False, slots=True)
class AsyncRedisComputeStateRepository:
    redis: AsyncRedisClient
    keys: ComputeStateKeys

    def __init__(
        self,
        redis: AsyncRedisClient,
        keys: ComputeStateKeys | None = None,
    ) -> None:
        self.redis = redis
        self.keys = keys or ComputeStateKeys(redis)

    async def save_agent_token_state(
        self,
        state: ComputeAgentTokenState,
        *,
        ttl_seconds: int = DEFAULT_COMPUTE_AGENT_TOKEN_TTL_SECONDS,
    ) -> ComputeAgentTokenState:
        payload = dump_model_json(state)
        await self.redis.set(self.keys.agent_token(state.token_hash), payload, ex=ttl_seconds)
        await self.redis.set(
            self.keys.agent_machine(
                state.workspace_id,
                state.capacity_owner_id,
                state.machine_id,
            ),
            payload,
        )
        await self.redis.set(
            self.keys.agent_machine_owner(state.workspace_id, state.machine_id),
            state.capacity_owner_id,
        )
        await self.redis.set_add(
            self.keys.agent_machine_index(state.workspace_id, state.capacity_owner_id),
            state.machine_id,
        )
        return state

    async def scan_agent_route_states(self) -> list[AgentBackendRoute]:
        raw_states = await self.redis.mget(await self.redis.scan(self.keys.agent_route_pattern()))
        states = [load_model_json(AgentBackendRoute, raw) for raw in raw_states if raw is not None]
        states.sort(key=_agent_route_order)
        return states

    async def delete_agent_route_state(
        self,
        *,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
        route_id: str,
    ) -> bool:
        deleted = bool(
            await self.redis.delete(
                self.keys.agent_route(
                    workspace_id,
                    capacity_owner_id,
                    machine_id,
                    route_id,
                )
            )
        )
        await self.redis.set_remove(
            self.keys.agent_route_index(workspace_id, capacity_owner_id, machine_id),
            route_id,
        )
        await self.redis.increment(
            self.keys.agent_route_revision(workspace_id, capacity_owner_id, machine_id)
        )
        return deleted
