from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import Field
from shared.capacity import CapacityPoolSizingSnapshot
from shared.compute_policy import ComputePoolRecord
from shared.contracts import ContractModel
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now


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


def capacity_pool_operational_health(
    capacity_owner_id: str,
    workers: Iterable[SchedulerWorkerRecord],
    *,
    state: CapacityPoolSizingSnapshot | None = None,
) -> CapacityPoolOperationalHealth:
    owner_workers = [worker for worker in workers if worker.capacity_owner_id == capacity_owner_id]
    if state is not None and state.consecutive_failures > 0:
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
    pool: ComputePoolRecord,
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
    pool: ComputePoolRecord,
    *,
    headroom: WorkerPoolEffectiveHeadroom,
    registered_units: int,
    authoritative_units: int,
    state: CapacityPoolSizingSnapshot,
    now: datetime | None = None,
) -> WorkerPoolSizingPlan:
    current_time = now or utc_now()
    sizing_state = state
    # Whether the pool ever reached its initial size has to outlive the workers
    # that proved it, or an idle pool drained to its minimum would be bought
    # straight back up to `initial_workers` on the next tick. The peak unit any
    # capacity operation ever asked for is that memory, and released rows keep it.
    initial_target_reached = (
        registered_units >= pool.initial_machines
        or sizing_state.peak_desired_units >= pool.initial_machines
    )
    current_units = max(registered_units, authoritative_units)
    baseline = max(
        pool.min_machines,
        0 if initial_target_reached else pool.initial_machines,
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
    if current_units >= pool.max_machines:
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
    retry_at = scale_up_retry_at(pool, sizing_state)
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
        target_units=min(current_units + 1, pool.max_machines),
        headroom=headroom,
        initial_target_reached=initial_target_reached,
        reason=(
            "worker count is below the configured baseline"
            if needs_baseline
            else "effective free headroom is below the configured minimum"
        ),
    )


def scale_up_retry_at(
    pool: ComputePoolRecord,
    state: CapacityPoolSizingSnapshot,
) -> datetime | None:
    """The earliest moment this pool may ask the provider for another unit.

    Nothing here is remembered between calls: the cooldowns run from the pool's
    own capacity operation timestamps and the backoff from the failure count
    recorded on the operation that failed. A scheduler that restarts mid-backoff
    therefore computes the same instant it would have computed anyway, instead of
    treating a pool whose launches all fail as one that has never failed.
    """

    candidates = [_failure_retry_at(pool, state)]
    if state.last_requested_at is not None:
        candidates.append(
            state.last_requested_at + timedelta(seconds=pool.scale_up_cooldown_seconds)
        )
    if state.last_released_at is not None:
        candidates.append(
            state.last_released_at + timedelta(seconds=pool.scale_up_cooldown_seconds)
        )
    return max((candidate for candidate in candidates if candidate is not None), default=None)


def _failure_retry_at(
    pool: ComputePoolRecord,
    state: CapacityPoolSizingSnapshot,
) -> datetime | None:
    if state.consecutive_failures <= 0 or state.last_failure_at is None:
        return None
    base_seconds = max(pool.scale_up_cooldown_seconds, 1)
    backoff_seconds = min(
        base_seconds * (2 ** (state.consecutive_failures - 1)),
        pool.registration_timeout_seconds,
    )
    return state.last_failure_at + timedelta(seconds=backoff_seconds)


def _below_minimum_headroom(
    pool: ComputePoolRecord,
    headroom: WorkerPoolEffectiveHeadroom,
) -> bool:
    return (
        headroom.cpu_millicores < pool.min_free_cpu_millicores
        or headroom.memory_mib < pool.min_free_memory_mib
        or headroom.gpu_count < pool.min_free_gpu_count
    )


def _worker_matches_pool_policy(pool: ComputePoolRecord, worker: SchedulerWorkerRecord) -> bool:
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
    "WorkerPoolEffectiveHeadroom",
    "WorkerPoolSizingAction",
    "WorkerPoolSizingPlan",
    "capacity_pool_operational_health",
    "capacity_pool_selection_key",
    "effective_pool_headroom",
    "plan_worker_pool_sizing",
    "scale_up_retry_at",
]
