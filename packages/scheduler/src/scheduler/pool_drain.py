from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from compute.providers import ProviderUnitSnapshot
from compute.service import ComputeService
from compute.state import ComputeUnitState, RedisComputeStateRepository
from pydantic import Field
from shared.compute_policy import ComputeUnitRecord, MachinePool, UnitName
from shared.contracts import ContractModel
from shared.env import truthy_env_value
from shared.errors import ConflictError
from shared.scheduling import SchedulerContainerStatus, SchedulerWorkerStatus
from shared.timestamps import utc_now


class WorkerPoolDrainAction(StrEnum):
    None_ = "none"
    ScaleWorkerPool = "scale-worker-pool"
    TerminateProviderMachine = "terminate-provider-machine"
    SurgeReplacementMachine = "surge-replacement-machine"
    CordonSupersededMachine = "cordon-superseded-machine"


class WorkerPoolDrainConfig(ContractModel):
    enabled: bool = True
    min_workers: int = 0
    idle_seconds: float = 300
    replace_drain_deadline_seconds: float = 3600
    """How long a cordoned machine may keep work that cannot be requeued.

    A cordon stops new work immediately and everything recoverable moves at once.
    This bounds the rest: without it one long container pins a node on a
    superseded release indefinitely, which is the drift replacement exists to
    remove. Pools running long batch work raise it rather than the default
    accommodating them.
    """


class WorkerPoolDrainResult(ContractModel):
    capacity_owner_id: str
    pool: MachinePool
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
    pool: MachinePool
    capacity_owner_id: str
    machine_id: str
    status: SchedulerWorkerStatus
    updated_at: datetime


class WorkerPoolDrainContainer(Protocol):
    container_id: str
    status: SchedulerContainerStatus


@runtime_checkable
class WorkerPoolDrainWorkerRepository(Protocol):
    def list_workers_for_capacity_owner(
        self,
        capacity_owner_id: str,
    ) -> Sequence[WorkerPoolDrainWorker]:
        """Workers this capacity owner holds.

        Asked by owner rather than by pool name: a unit's name and the pool its
        workers register under are different strings, and asking for one by the
        other returns nothing at all rather than failing.
        """
        ...


@runtime_checkable
class WorkerPoolDrainContainerRepository(Protocol):
    def list_by_worker(self, worker_id: str) -> Sequence[WorkerPoolDrainContainer]: ...


class WorkerPoolDrainCapacityOwner(Protocol):
    """The capacity owner's mutation lease, and what is already claimed against it.

    One lease, taken here and re-entered by whatever this decision calls: a surge
    scales the unit, and scaling takes the same owner's lease for itself.
    """

    def mutation_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...

    def has_open_reservations(self, capacity_owner_id: str) -> bool: ...


class WorkerPoolDrainController(Protocol):
    @property
    def capacity_owner_id(self) -> str: ...

    @property
    def unit_name(self) -> UnitName: ...

    @property
    def pool(self) -> MachinePool: ...

    def observe(self) -> WorkerPoolDrainObservation: ...

    def reconcile(
        self,
        observation: WorkerPoolDrainObservation,
        *,
        now: datetime | None = None,
    ) -> WorkerPoolDrainResult: ...


@dataclass(frozen=True, slots=True)
class WorkerPoolDrainObservation:
    unit: ComputeUnitRecord
    snapshot: ProviderUnitSnapshot


@dataclass(slots=True)
class WorkerPoolDrainService:
    controllers: Callable[[], Sequence[WorkerPoolDrainController]]
    capacity_owners: WorkerPoolDrainCapacityOwner

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
        try:
            observation = controller.observe()
        except Exception as exc:
            return WorkerPoolDrainResult(
                capacity_owner_id=controller.capacity_owner_id,
                pool=controller.pool,
                reason="worker-pool observation failed",
                lock_acquired=False,
                error=str(exc),
            )
        try:
            with self.capacity_owners.mutation_lock(controller.capacity_owner_id):
                if self.capacity_owners.has_open_reservations(controller.capacity_owner_id):
                    return WorkerPoolDrainResult(
                        capacity_owner_id=controller.capacity_owner_id,
                        pool=controller.pool,
                        reason="capacity owner has open provisioning allocations",
                    )
                return controller.reconcile(observation, now=current_time)
        except ConflictError as conflict:
            return WorkerPoolDrainResult(
                capacity_owner_id=controller.capacity_owner_id,
                pool=controller.pool,
                reason="capacity-owner mutation lock already held",
                lock_acquired=False,
                error=str(conflict),
            )
        except Exception as exc:
            return WorkerPoolDrainResult(
                capacity_owner_id=controller.capacity_owner_id,
                pool=controller.pool,
                reason="worker-pool drain failed",
                error=str(exc),
            )


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
    def pool(self) -> MachinePool:
        return self.state.pool

    @property
    def capacity_owner_id(self) -> str:
        return _capacity_owner_id_from_state(self.state)

    def observe(self) -> WorkerPoolDrainObservation:
        unit, snapshot = self.compute.inspect_internal_unit(
            self.state.workspace_id,
            self.capacity_owner_id,
        )
        return WorkerPoolDrainObservation(
            unit=unit,
            snapshot=snapshot,
        )

    def reconcile(
        self,
        observation: WorkerPoolDrainObservation,
        *,
        now: datetime | None = None,
    ) -> WorkerPoolDrainResult:
        current_time = now or utc_now()
        current_unit = self.compute.get_internal_unit(
            self.state.workspace_id,
            self.capacity_owner_id,
        )
        if (
            current_unit.id != observation.unit.id
            or current_unit.generation != observation.unit.generation
        ):
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                reason="worker-pool capacity changed during provider observation",
            )
        config = _drain_config_from_state(self.state)
        if not config.enabled:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                reason="worker-pool drain disabled",
            )
        replacement = self._reconcile_replacement(
            config,
            snapshot=observation.snapshot,
            now=current_time,
        )
        if replacement is not None:
            return replacement
        sizing_state = self.compute.pool_sizing_snapshot(self.capacity_owner_id)
        if sizing_state.pending_operation_id or sizing_state.desired_units > (
            self.state.active_machines
        ):
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
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
                pool=self.pool,
                reason="worker-pool scale-down cooldown is active",
            )
        if self.state.active_machines <= config.min_workers:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                desired_replicas=self.state.desired_machines,
                observed_replicas=self.state.active_machines,
                reason="pool is at min machines",
            )
        workers_by_machine = _workers_by_machine(
            self.workers.list_workers_for_capacity_owner(self.capacity_owner_id)
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
                pool=self.pool,
                desired_replicas=self.state.desired_machines,
                observed_replicas=self.state.active_machines,
                reason="no idle provider machine candidate",
            )
        pooled = self.compute.release_internal_unit_machine(
            self.state.workspace_id,
            self.capacity_owner_id,
            candidate.machine_id,
        )
        desired_replicas = pooled.desired_machines
        observed_replicas = pooled.observed_machines
        reason = "released idle connected provider machine"
        return WorkerPoolDrainResult(
            capacity_owner_id=self.capacity_owner_id,
            pool=self.pool,
            action=WorkerPoolDrainAction.TerminateProviderMachine,
            machine_id=candidate.machine_id,
            desired_replicas=desired_replicas,
            observed_replicas=observed_replicas,
            drained_worker_ids=[worker.worker_id for worker in candidate.workers],
            reason=reason,
        )

    def _reconcile_replacement(
        self,
        config: WorkerPoolDrainConfig,
        *,
        snapshot: ProviderUnitSnapshot,
        now: datetime,
    ) -> WorkerPoolDrainResult | None:
        """Move the pool onto the version it would launch today, one machine at a time.

        Surging before cordoning is what lets a pool at `min_machines` update
        itself: the idle-drain phase below refuses to go under that floor, and
        adding first means active is above it by the time anything is removed.

        Returns None when there is nothing superseded, so the idle-drain phase
        runs as it did before.
        """
        current_version = snapshot.current_template_version
        if not current_version:
            # The provider cannot say what it would launch. Nothing is provably
            # stale, and treating that as "everything" would replace a whole pool
            # on a provider that simply does not report versions.
            return None

        machines_by_instance = self.compute.internal_unit_machine_by_instance(
            self.state.workspace_id,
            self.capacity_owner_id,
        )
        superseded = [
            machine_id
            for instance in snapshot.instances
            if instance.booted_template_version
            and instance.booted_template_version != current_version
            and (machine_id := machines_by_instance.get(instance.provider_instance_id))
        ]
        if not superseded:
            return None

        workers_by_machine = _workers_by_machine(
            self.workers.list_workers_for_capacity_owner(self.capacity_owner_id)
        )
        cordoned_since = self.compute.internal_unit_cordoned_machines(
            self.state.workspace_id,
            self.capacity_owner_id,
        )
        cordoned = [machine_id for machine_id in superseded if machine_id in cordoned_since]
        if cordoned:
            return self._release_cordoned(
                cordoned[0],
                workers_by_machine,
                config,
                cordoned_at=cordoned_since[cordoned[0]],
                now=now,
            )

        # One at a time. A template change otherwise cordons a whole pool at once,
        # and every machine surges a replacement beside it.
        sizing_state = self.compute.pool_sizing_snapshot(self.capacity_owner_id)
        if sizing_state.pending_operation_id:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                reason="replacement is waiting on a capacity operation",
            )
        if self.state.active_machines != self.state.desired_machines:
            # Cordoning while the count is moving would take capacity away before
            # the replacement it was surged for has arrived. Under is the surge
            # still booting; over is a release still settling.
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                reason=(
                    "replacement machine has not registered"
                    if self.state.active_machines < self.state.desired_machines
                    else "released replacement capacity is still settling"
                ),
            )

        up_to_date = [
            instance
            for instance in snapshot.instances
            if instance.booted_template_version == current_version
        ]
        if not up_to_date:
            # Nothing in the pool can take this machine's work yet. Cordoning now
            # would leave a pool at its minimum with nowhere to place anything
            # until the replacement finishes booting.
            return self._surge_for_replacement(superseded[0], current_version)
        return self._cordon_superseded(superseded[0], snapshot, current_version, now=now)

    def _surge_for_replacement(
        self,
        machine_id: str,
        current_version: str,
    ) -> WorkerPoolDrainResult:
        """Add the replacement before taking anything away, or report it exists."""
        if self.state.active_machines >= self.state.max_machines:
            # No room to surge. Nothing is cordoned, so the pool keeps serving on
            # the old version rather than shrinking to make room.
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                reason="replacement cannot surge past the pool maximum",
            )
        target = self.state.active_machines + 1
        self.compute.scale_internal_unit(
            self.state.workspace_id,
            self.capacity_owner_id,
            target,
            before_mutation=lambda _unit: None,
        )
        return WorkerPoolDrainResult(
            capacity_owner_id=self.capacity_owner_id,
            pool=self.pool,
            action=WorkerPoolDrainAction.SurgeReplacementMachine,
            machine_id=machine_id,
            desired_replicas=target,
            observed_replicas=self.state.active_machines,
            reason=f"surged a replacement for template {current_version}",
        )

    def _cordon_superseded(
        self,
        machine_id: str,
        snapshot: ProviderUnitSnapshot,
        current_version: str,
        *,
        now: datetime,
    ) -> WorkerPoolDrainResult | None:
        booted = next(
            (
                instance.booted_template_version
                for instance in snapshot.instances
                if instance.booted_template_version
                and instance.booted_template_version != current_version
            ),
            "",
        )
        changed = self.compute.cordon_internal_unit_machine(
            self.state.workspace_id,
            machine_id,
            reason=f"launch template {booted} superseded by {current_version}",
            now=now,
        )
        if not changed:
            return None
        return WorkerPoolDrainResult(
            capacity_owner_id=self.capacity_owner_id,
            pool=self.pool,
            action=WorkerPoolDrainAction.CordonSupersededMachine,
            machine_id=machine_id,
            desired_replicas=self.state.desired_machines,
            observed_replicas=self.state.active_machines,
            reason=f"cordoned template {booted}, superseded by {current_version}",
        )

    def _release_cordoned(
        self,
        machine_id: str,
        workers_by_machine: dict[str, list[WorkerPoolDrainWorker]],
        config: WorkerPoolDrainConfig,
        *,
        cordoned_at: datetime,
        now: datetime,
    ) -> WorkerPoolDrainResult | None:
        """Take a cordoned machine away once it is empty, or once its time is up.

        Emptying is the normal case and needs no deadline: the cordon moved every
        recoverable request the moment it was applied. The deadline only bounds
        what could not be moved, and one long container would otherwise pin a
        machine on an old release for as long as it runs.
        """
        workers = workers_by_machine.get(machine_id, [])
        if _pool_has_active_containers(workers, self.containers):
            deadline = cordoned_at + timedelta(seconds=config.replace_drain_deadline_seconds)
            if now < deadline:
                return WorkerPoolDrainResult(
                    capacity_owner_id=self.capacity_owner_id,
                    pool=self.pool,
                    machine_id=machine_id,
                    reason="cordoned machine is still draining",
                )
        pooled = self.compute.release_internal_unit_machine(
            self.state.workspace_id,
            self.capacity_owner_id,
            machine_id,
        )
        return WorkerPoolDrainResult(
            capacity_owner_id=self.capacity_owner_id,
            pool=self.pool,
            action=WorkerPoolDrainAction.TerminateProviderMachine,
            machine_id=machine_id,
            desired_replicas=pooled.desired_machines,
            observed_replicas=pooled.observed_machines,
            drained_worker_ids=[worker.worker_id for worker in workers],
            reason="released a machine on a superseded template",
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
        replace_drain_deadline_seconds=_float_label(
            labels,
            "replace_drain_deadline_seconds",
            3600,
        ),
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
