from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from compute.service import ComputeService
from compute.state import ComputeUnitState, RedisComputeStateRepository
from coordination.redis_client import RedisClient
from pydantic import Field
from shared.compute_policy import UnitName
from shared.contracts import ContractModel
from shared.env import truthy_env_value
from shared.scheduling import SchedulerContainerStatus, SchedulerWorkerStatus
from shared.timestamps import utc_now

from scheduler.state import WorkerPoolLockKind, WorkerPoolLockPlan

DEFAULT_WORKER_POOL_DRAIN_OWNER = "scheduler"


class WorkerPoolDrainAction(StrEnum):
    None_ = "none"
    ScaleWorkerPool = "scale-worker-pool"
    TerminateProviderMachine = "terminate-provider-machine"


class WorkerPoolDrainConfig(ContractModel):
    enabled: bool = True
    min_workers: int = 0
    idle_seconds: float = 300


class WorkerPoolDrainResult(ContractModel):
    capacity_owner_id: str
    pool: str
    action: WorkerPoolDrainAction = WorkerPoolDrainAction.None_
    machine_id: str = ""
    desired_replicas: int = 0
    observed_replicas: int = 0
    drained_worker_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    lock_acquired: bool = True
    error: str = ""


class WorkerPoolDrainWorker(Protocol):
    worker_id: str
    pool: str
    capacity_owner_id: str
    machine_id: str
    status: SchedulerWorkerStatus
    updated_at: datetime


class WorkerPoolDrainContainer(Protocol):
    container_id: str
    status: SchedulerContainerStatus


@runtime_checkable
class WorkerPoolDrainWorkerRepository(Protocol):
    def list_workers_in_pool(self, pool: str) -> Sequence[WorkerPoolDrainWorker]: ...


@runtime_checkable
class WorkerPoolDrainContainerRepository(Protocol):
    def list_by_worker(self, worker_id: str) -> Sequence[WorkerPoolDrainContainer]: ...


class WorkerPoolDrainLockRepository(Protocol):
    def lock_plan(
        self,
        capacity_owner_id: str,
        kind: WorkerPoolLockKind,
    ) -> WorkerPoolLockPlan: ...


class WorkerPoolDrainReservationGuard(Protocol):
    def has_open_reservations(self, capacity_owner_id: str) -> bool: ...


class WorkerPoolDrainController(Protocol):
    @property
    def capacity_owner_id(self) -> str: ...

    @property
    def unit_name(self) -> UnitName: ...

    def reconcile(self, *, now: datetime | None = None) -> WorkerPoolDrainResult: ...


@dataclass(slots=True)
class WorkerPoolDrainService:
    redis: RedisClient
    locks: WorkerPoolDrainLockRepository
    controllers: Callable[[], Sequence[WorkerPoolDrainController]]
    reservations: WorkerPoolDrainReservationGuard

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[WorkerPoolDrainResult]:
        current_time = now or utc_now()
        results: list[WorkerPoolDrainResult] = []
        for controller in self.controllers()[: max(limit, 0)]:
            results.append(self.reconcile_controller(controller, now=current_time))
        return results

    def reconcile_controller(
        self,
        controller: WorkerPoolDrainController,
        *,
        now: datetime | None = None,
    ) -> WorkerPoolDrainResult:
        current_time = now or utc_now()
        lock_plan = self.locks.lock_plan(
            controller.capacity_owner_id,
            WorkerPoolLockKind.Sizer,
        )
        token = f"{DEFAULT_WORKER_POOL_DRAIN_OWNER}:{current_time.timestamp()}"
        if not self._acquire_lock(lock_plan, token):
            return WorkerPoolDrainResult(
                capacity_owner_id=controller.capacity_owner_id,
                pool=controller.unit_name,
                reason="capacity-owner mutation lock already held",
                lock_acquired=False,
            )
        try:
            if self.reservations.has_open_reservations(controller.capacity_owner_id):
                return WorkerPoolDrainResult(
                    capacity_owner_id=controller.capacity_owner_id,
                    pool=controller.unit_name,
                    reason="capacity owner has open provisioning allocations",
                )
            return controller.reconcile(now=current_time)
        except Exception as exc:
            return WorkerPoolDrainResult(
                capacity_owner_id=controller.capacity_owner_id,
                pool=controller.unit_name,
                reason="worker-pool drain failed",
                error=str(exc),
            )
        finally:
            self._release_lock(lock_plan, token)

    def _acquire_lock(self, plan: WorkerPoolLockPlan, token: str) -> bool:
        return bool(self.redis.set(plan.key, token, nx=True, ex=plan.ttl_seconds))

    def _release_lock(self, plan: WorkerPoolLockPlan, token: str) -> None:
        if self.redis.get(plan.key) == token:
            self.redis.delete(plan.key)


@dataclass(slots=True)
class ManagedComputeWorkerPoolDrainController:
    state: ComputeUnitState
    compute: ComputeService
    workers: WorkerPoolDrainWorkerRepository
    containers: WorkerPoolDrainContainerRepository

    @property
    def unit_name(self) -> UnitName:
        return self.state.name

    @property
    def capacity_owner_id(self) -> str:
        return _capacity_owner_id_from_state(self.state)

    def reconcile(self, *, now: datetime | None = None) -> WorkerPoolDrainResult:
        current_time = now or utc_now()
        config = _drain_config_from_state(self.state)
        if not config.enabled:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.unit_name,
                reason="worker-pool drain disabled",
            )
        sizing_state = self.compute.pool_sizing_snapshot(self.capacity_owner_id)
        if sizing_state.pending_operation_id or sizing_state.desired_units > (
            self.state.active_machines
        ):
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.unit_name,
                reason="worker-pool capacity is awaiting registration",
            )
        latest_mutation = max(
            (
                changed_at
                for changed_at in (
                    sizing_state.last_requested_at,
                    sizing_state.last_released_at,
                )
                if changed_at is not None
            ),
            default=None,
        )
        cooldown_seconds = _int_sizing_label(
            self.state,
            "scale_down_cooldown_seconds",
            60,
        )
        if (
            latest_mutation is not None
            and latest_mutation + timedelta(seconds=cooldown_seconds) > current_time
        ):
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.unit_name,
                reason="worker-pool scale-down cooldown is active",
            )
        if self.state.active_machines <= config.min_workers:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.unit_name,
                desired_replicas=self.state.desired_machines,
                observed_replicas=self.state.active_machines,
                reason="pool is at min machines",
            )
        workers_by_machine = _workers_by_machine(
            [
                worker
                for worker in self.workers.list_workers_in_pool(self.unit_name)
                if worker.capacity_owner_id == self.capacity_owner_id
            ]
        )
        candidate = _idle_machine_candidate(
            workers_by_machine,
            self.containers,
            now=current_time,
            idle_seconds=config.idle_seconds,
        )
        if candidate is None:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.unit_name,
                desired_replicas=self.state.desired_machines,
                observed_replicas=self.state.active_machines,
                reason="no idle provider machine candidate",
            )
        pooled = self.compute.release_internal_unit_machine(
            self.state.workspace_id,
            self.unit_name,
            candidate.machine_id,
        )
        desired_replicas = pooled.desired_machines
        observed_replicas = pooled.observed_machines
        reason = "released idle connected provider machine"
        return WorkerPoolDrainResult(
            capacity_owner_id=self.capacity_owner_id,
            pool=self.unit_name,
            action=WorkerPoolDrainAction.TerminateProviderMachine,
            machine_id=candidate.machine_id,
            desired_replicas=desired_replicas,
            observed_replicas=observed_replicas,
            drained_worker_ids=[worker.worker_id for worker in candidate.workers],
            reason=reason,
        )


def managed_compute_drain_controllers(
    compute: ComputeService,
    compute_states: RedisComputeStateRepository,
    workers: WorkerPoolDrainWorkerRepository,
    containers: WorkerPoolDrainContainerRepository,
) -> list[ManagedComputeWorkerPoolDrainController]:
    controllers: list[ManagedComputeWorkerPoolDrainController] = []
    for state in compute_states.list_all_pool_states():
        if not _managed_compute_pool_state(state):
            continue
        controllers.append(
            ManagedComputeWorkerPoolDrainController(state, compute, workers, containers)
        )
    controllers.sort(key=lambda item: item.unit_name)
    return controllers


def _drain_config_from_state(state: ComputeUnitState) -> WorkerPoolDrainConfig:
    metadata = state.metadata
    raw = metadata.get("drain") if isinstance(metadata.get("drain"), dict) else {}
    labels = {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}
    sizing = metadata.get("sizing") if isinstance(metadata.get("sizing"), dict) else {}
    if isinstance(sizing, dict):
        labels.update({str(key): str(value) for key, value in sizing.items()})
    enabled = labels.get("scale_down_enabled", labels.get("drain_enabled", "true"))
    return WorkerPoolDrainConfig(
        enabled=truthy_env_value(enabled),
        min_workers=max(state.min_machines, 0),
        idle_seconds=_float_label(labels, "scale_down_idle_seconds", 300),
    )


def _int_sizing_label(state: ComputeUnitState, key: str, default: int) -> int:
    sizing = state.metadata.get("sizing")
    if not isinstance(sizing, dict):
        return default
    raw = sizing.get(key)
    if not isinstance(raw, (str, int, float)):
        return default
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return default


def _capacity_owner_id_from_state(state: ComputeUnitState) -> str:
    return state.capacity_owner_id


def _managed_compute_pool_state(state: ComputeUnitState) -> bool:
    raw_config = state.metadata.get("config")
    if not isinstance(raw_config, dict):
        return state.provider not in {"", "agent", "local"}
    providers = raw_config.get("providers")
    return isinstance(providers, list) and bool(providers)


def _pool_has_active_containers(
    workers: Sequence[WorkerPoolDrainWorker],
    containers: WorkerPoolDrainContainerRepository,
) -> bool:
    return any(
        container.status
        in {
            SchedulerContainerStatus.Pending,
            SchedulerContainerStatus.Running,
            SchedulerContainerStatus.Stopping,
        }
        for worker in workers
        for container in containers.list_by_worker(worker.worker_id)
    )


@dataclass(slots=True)
class _IdleMachineCandidate:
    machine_id: str
    workers: list[WorkerPoolDrainWorker]


def _workers_by_machine(
    workers: Sequence[WorkerPoolDrainWorker],
) -> dict[str, list[WorkerPoolDrainWorker]]:
    grouped: dict[str, list[WorkerPoolDrainWorker]] = {}
    for worker in workers:
        machine_id = getattr(worker, "machine_id", "")
        if not machine_id:
            continue
        grouped.setdefault(machine_id, []).append(worker)
    return grouped


def _idle_machine_candidate(
    workers_by_machine: dict[str, list[WorkerPoolDrainWorker]],
    containers: WorkerPoolDrainContainerRepository,
    *,
    now: datetime,
    idle_seconds: float,
) -> _IdleMachineCandidate | None:
    candidates: list[_IdleMachineCandidate] = []
    for machine_id, workers in workers_by_machine.items():
        if _pool_has_active_containers(workers, containers):
            continue
        idle_workers = _idle_workers(workers, now, idle_seconds)
        if len(idle_workers) != len(workers):
            continue
        candidates.append(_IdleMachineCandidate(machine_id=machine_id, workers=idle_workers))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: min(worker.updated_at for worker in item.workers))[0]


def _idle_workers(
    workers: Sequence[WorkerPoolDrainWorker],
    now: datetime,
    idle_seconds: float,
) -> list[WorkerPoolDrainWorker]:
    return [
        worker
        for worker in sorted(workers, key=lambda item: item.updated_at)
        if worker.status
        in {
            SchedulerWorkerStatus.Available,
            SchedulerWorkerStatus.Draining,
            SchedulerWorkerStatus.Unavailable,
        }
        and (now - worker.updated_at).total_seconds() >= idle_seconds
    ]


def _float_label(labels: dict[str, str], key: str, default: float) -> float:
    raw = labels.get(key, labels.get(f"drain.{key}", ""))
    if raw == "":
        return default
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return default
