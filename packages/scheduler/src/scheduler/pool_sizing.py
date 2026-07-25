from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from coordination.redis_client import RedisClient, redis_text
from pydantic import Field
from shared.capacity import (
    CapacityPoolPolicy,
    CapacityPoolSizingState,
    CapacityPoolSizingStateUpdate,
)
from shared.compute_fleet import Pool
from shared.contracts import ContractModel
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now


class WorkerPoolReplicaScaleOutcome(StrEnum):
    ExistingPending = "existing_pending"
    Requested = "requested"
    AtLimit = "at_limit"
    TemporarilyUnavailable = "temporarily_unavailable"
    Unsupported = "unsupported"


class WorkerPoolReplicaScaleResult(ContractModel):
    outcome: WorkerPoolReplicaScaleOutcome
    capacity_owner_id: str
    pool_name: str
    desired_replicas: int
    observed_replicas: int = 0
    resource_version: str = ""
    provider: str = ""
    target: str = ""
    retry_after_seconds: float = 0
    reason: str


class WorkerPoolReplicaState(ContractModel):
    capacity_owner_id: str
    pool_name: str
    provider: str = ""
    desired_replicas: int = 0
    observed_replicas: int = 0
    target: str = ""
    updated_at: datetime = Field(default_factory=utc_now)


class CapacityPoolOperationalHealth(StrEnum):
    Healthy = "healthy"
    Degraded = "degraded"
    Unavailable = "unavailable"


class WorkerPoolSizingAction(StrEnum):
    None_ = "none"
    Wait = "wait"
    ScaleUp = "scale_up"


class WorkerPoolEffectiveHeadroom(ContractModel):
    cpu_millicores: int = 0
    memory_mib: int = 0
    gpu_count: int = 0
    available_workers: int = 0
    unclaimed_pending_workers: int = 0
    active_allocations: int = 0


class WorkerPoolSizingPlan(ContractModel):
    action: WorkerPoolSizingAction
    capacity_owner_id: str
    pool_name: str
    current_units: int = Field(ge=0)
    target_units: int = Field(ge=0)
    headroom: WorkerPoolEffectiveHeadroom
    initial_target_reached: bool = False
    retry_after_seconds: float = Field(default=0, ge=0)
    reason: str


class WorkerPoolSizingReservation(Protocol):
    @property
    def capacity_owner_id(self) -> str: ...

    @property
    def target_worker_id(self) -> str: ...

    @property
    def desired_unit(self) -> int: ...

    @property
    def open(self) -> bool: ...


class WorkerPoolSizingAllocation(Protocol):
    cpu_millicores: int
    memory_mib: int
    gpu_count: int


class WorkerPoolReplicaScaler(Protocol):
    def describe_worker_pool(
        self,
        pool: Pool,
        *,
        reservation_id: str,
        operation_id: str,
    ) -> WorkerPoolReplicaScaleResult: ...

    def scale_worker_pool(
        self,
        pool: Pool,
        replicas: int,
        *,
        reservation_id: str,
        operation_id: str,
    ) -> WorkerPoolReplicaScaleResult: ...


class WorkerPoolReplicaStateStore(Protocol):
    def get_state(self, capacity_owner_id: str) -> WorkerPoolReplicaState | None: ...

    def save_state(self, state: WorkerPoolReplicaState) -> WorkerPoolReplicaState: ...


class CapacityPoolSizingStateService(Protocol):
    def get_pool_sizing_state(self, capacity_owner_id: str) -> CapacityPoolSizingState: ...

    def compare_and_set_pool_sizing_state(
        self,
        update: CapacityPoolSizingStateUpdate,
    ) -> CapacityPoolSizingState: ...


@dataclass(slots=True)
class RedisWorkerPoolReplicaStateStore:
    redis: RedisClient
    namespace: str = "scheduler"

    def get_state(self, capacity_owner_id: str) -> WorkerPoolReplicaState | None:
        raw = self.redis.get(self._key(capacity_owner_id))
        if not raw:
            return None
        return WorkerPoolReplicaState.model_validate_json(redis_text(raw))

    def save_state(self, state: WorkerPoolReplicaState) -> WorkerPoolReplicaState:
        stored = state.model_copy(update={"updated_at": utc_now()})
        self.redis.set(self._key(stored.capacity_owner_id), stored.model_dump_json())
        return stored

    def _key(self, capacity_owner_id: str) -> str:
        return self.redis.key(
            self.namespace,
            "capacity-owners",
            capacity_owner_id,
            "replicas",
        )


def capacity_pool_operational_health(
    capacity_owner_id: str,
    workers: Iterable[SchedulerWorkerRecord],
    *,
    state: CapacityPoolSizingState | None = None,
    now: datetime | None = None,
) -> CapacityPoolOperationalHealth:
    current_time = now or utc_now()
    owner_workers = [worker for worker in workers if worker.capacity_owner_id == capacity_owner_id]
    if (
        state is not None
        and state.retry_after_at is not None
        and state.retry_after_at > current_time
    ):
        return CapacityPoolOperationalHealth.Degraded
    if state is not None and state.terminal_reason:
        return CapacityPoolOperationalHealth.Degraded
    if any(worker.status is SchedulerWorkerStatus.Available for worker in owner_workers):
        return CapacityPoolOperationalHealth.Healthy
    if any(worker.status is SchedulerWorkerStatus.Pending for worker in owner_workers):
        return CapacityPoolOperationalHealth.Degraded
    return CapacityPoolOperationalHealth.Unavailable


def capacity_pool_selection_key(
    *,
    health: CapacityPoolOperationalHealth,
    priority: int,
    capacity_owner_id: str,
) -> tuple[int, int, str]:
    health_order = {
        CapacityPoolOperationalHealth.Healthy: 0,
        CapacityPoolOperationalHealth.Degraded: 1,
        CapacityPoolOperationalHealth.Unavailable: 2,
    }
    return health_order[health], -priority, capacity_owner_id


def effective_pool_headroom(
    pool: Pool,
    workers: Iterable[SchedulerWorkerRecord],
    *,
    reservations: Iterable[WorkerPoolSizingReservation] = (),
    allocations: Iterable[WorkerPoolSizingAllocation] = (),
) -> WorkerPoolEffectiveHeadroom:
    claimed_pending_workers = {
        reservation.target_worker_id
        for reservation in reservations
        if reservation.open
        and reservation.capacity_owner_id == pool.capacity_owner_id
        and reservation.target_worker_id
    }
    available: list[SchedulerWorkerRecord] = []
    pending: list[SchedulerWorkerRecord] = []
    for worker in workers:
        if not _worker_matches_pool_policy(pool, worker):
            continue
        if worker.status is SchedulerWorkerStatus.Available:
            available.append(worker)
        elif (
            worker.status is SchedulerWorkerStatus.Pending
            and worker.worker_id not in claimed_pending_workers
        ):
            pending.append(worker)
    active_allocations = list(allocations)
    return WorkerPoolEffectiveHeadroom(
        cpu_millicores=(
            sum(worker.free_cpu_millicores for worker in [*available, *pending])
            - sum(allocation.cpu_millicores for allocation in active_allocations)
        ),
        memory_mib=(
            sum(worker.free_memory_mib for worker in [*available, *pending])
            - sum(allocation.memory_mib for allocation in active_allocations)
        ),
        gpu_count=(
            sum(worker.free_gpu_count for worker in [*available, *pending])
            - sum(allocation.gpu_count for allocation in active_allocations)
        ),
        available_workers=len(available),
        unclaimed_pending_workers=len(pending),
        active_allocations=len(active_allocations),
    )


def plan_worker_pool_sizing(
    pool: Pool,
    *,
    headroom: WorkerPoolEffectiveHeadroom,
    registered_units: int,
    authoritative_units: int,
    state: CapacityPoolSizingState,
    now: datetime | None = None,
) -> WorkerPoolSizingPlan:
    current_time = now or utc_now()
    sizing_state = state
    initial_target_reached = (
        sizing_state.initial_target_reached or registered_units >= pool.initial_workers
    )
    current_units = max(registered_units, authoritative_units)
    baseline = max(
        pool.min_workers,
        0 if initial_target_reached else pool.initial_workers,
    )
    if not pool.scaling_enabled:
        return WorkerPoolSizingPlan(
            action=WorkerPoolSizingAction.None_,
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
            current_units=current_units,
            target_units=current_units,
            headroom=headroom,
            initial_target_reached=initial_target_reached,
            reason="worker-pool scaling is disabled",
        )
    if sizing_state.operation_id and registered_units < sizing_state.target_units:
        return WorkerPoolSizingPlan(
            action=WorkerPoolSizingAction.Wait,
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
            current_units=current_units,
            target_units=sizing_state.target_units,
            headroom=headroom,
            initial_target_reached=initial_target_reached,
            reason="waiting for the persisted sizing operation to register",
        )
    if current_units > registered_units:
        return WorkerPoolSizingPlan(
            action=WorkerPoolSizingAction.Wait,
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
            current_units=current_units,
            target_units=current_units,
            headroom=headroom,
            initial_target_reached=initial_target_reached,
            reason="authoritative capacity is awaiting worker registration",
        )
    needs_baseline = registered_units < baseline
    needs_headroom = _below_minimum_headroom(pool, headroom)
    if not needs_baseline and not needs_headroom:
        return WorkerPoolSizingPlan(
            action=WorkerPoolSizingAction.None_,
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
            current_units=current_units,
            target_units=current_units,
            headroom=headroom,
            initial_target_reached=initial_target_reached,
            reason="worker-pool baseline and free headroom are satisfied",
        )
    if current_units >= pool.max_workers:
        return WorkerPoolSizingPlan(
            action=WorkerPoolSizingAction.None_,
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
            current_units=current_units,
            target_units=current_units,
            headroom=headroom,
            initial_target_reached=initial_target_reached,
            reason="worker-pool maximum is exhausted",
        )
    retry_at = _scale_up_retry_at(pool, sizing_state)
    if retry_at is not None and retry_at > current_time:
        return WorkerPoolSizingPlan(
            action=WorkerPoolSizingAction.Wait,
            capacity_owner_id=pool.capacity_owner_id,
            pool_name=pool.name,
            current_units=current_units,
            target_units=current_units,
            headroom=headroom,
            initial_target_reached=initial_target_reached,
            retry_after_seconds=(retry_at - current_time).total_seconds(),
            reason="worker-pool scale-up cooldown or retry backoff is active",
        )
    return WorkerPoolSizingPlan(
        action=WorkerPoolSizingAction.ScaleUp,
        capacity_owner_id=pool.capacity_owner_id,
        pool_name=pool.name,
        current_units=current_units,
        target_units=min(current_units + 1, pool.max_workers),
        headroom=headroom,
        initial_target_reached=initial_target_reached,
        reason=(
            "worker count is below the configured baseline"
            if needs_baseline
            else "effective free headroom is below the configured minimum"
        ),
    )


def sizing_failure_state(
    state: CapacityPoolSizingState,
    pool: CapacityPoolPolicy,
    *,
    now: datetime,
) -> CapacityPoolSizingState:
    failures = state.consecutive_failures + 1
    base_seconds = max(pool.scale_up_cooldown_seconds, 1)
    backoff_seconds = min(base_seconds * (2 ** (failures - 1)), pool.registration_timeout_seconds)
    return state.model_copy(
        update={
            "consecutive_failures": failures,
            "retry_after_at": now + timedelta(seconds=backoff_seconds),
        }
    )


def sizing_state_update(state: CapacityPoolSizingState) -> CapacityPoolSizingStateUpdate:
    return CapacityPoolSizingStateUpdate(
        capacity_owner_id=state.capacity_owner_id,
        expected_revision=state.revision,
        initial_target_reached=state.initial_target_reached,
        operation_id=state.operation_id,
        target_units=state.target_units,
        operation_started_at=state.operation_started_at,
        last_scale_up_at=state.last_scale_up_at,
        last_scale_down_at=state.last_scale_down_at,
        retry_after_at=state.retry_after_at,
        consecutive_failures=state.consecutive_failures,
        terminal_reason=state.terminal_reason,
    )


def _scale_up_retry_at(
    pool: CapacityPoolPolicy,
    state: CapacityPoolSizingState,
) -> datetime | None:
    candidates = [state.retry_after_at]
    if state.last_scale_up_at is not None:
        candidates.append(
            state.last_scale_up_at + timedelta(seconds=pool.scale_up_cooldown_seconds)
        )
    if state.last_scale_down_at is not None:
        candidates.append(
            state.last_scale_down_at + timedelta(seconds=pool.scale_up_cooldown_seconds)
        )
    return max((candidate for candidate in candidates if candidate is not None), default=None)


def _below_minimum_headroom(
    pool: CapacityPoolPolicy,
    headroom: WorkerPoolEffectiveHeadroom,
) -> bool:
    return (
        headroom.cpu_millicores < pool.min_free_cpu_millicores
        or headroom.memory_mib < pool.min_free_memory_mib
        or headroom.gpu_count < pool.min_free_gpu_count
    )


def _worker_matches_pool_policy(pool: Pool, worker: SchedulerWorkerRecord) -> bool:
    return (
        worker.capacity_owner_id == pool.capacity_owner_id
        and worker.total_cpu_millicores > 0
        and worker.total_memory_mib > 0
        and worker.total_gpu_count >= pool.worker_gpu_count
        and (pool.worker_gpu_count == 0 or worker.gpu_type == pool.worker_gpu_type)
        and all(runtime in worker.runtime_classes for runtime in pool.worker_runtimes)
        and worker.preemptible is pool.worker_preemptible
    )


__all__ = [
    "CapacityPoolOperationalHealth",
    "CapacityPoolSizingStateService",
    "RedisWorkerPoolReplicaStateStore",
    "WorkerPoolEffectiveHeadroom",
    "WorkerPoolReplicaScaleOutcome",
    "WorkerPoolReplicaScaleResult",
    "WorkerPoolReplicaScaler",
    "WorkerPoolReplicaState",
    "WorkerPoolReplicaStateStore",
    "WorkerPoolSizingAction",
    "WorkerPoolSizingPlan",
    "capacity_pool_operational_health",
    "capacity_pool_selection_key",
    "effective_pool_headroom",
    "plan_worker_pool_sizing",
    "sizing_failure_state",
    "sizing_state_update",
]
