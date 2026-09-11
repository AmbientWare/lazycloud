from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import ceil
from typing import Protocol, runtime_checkable

from compute.provider_machines import provider_unit_operational_capacity
from compute.providers import ProviderUnitSnapshot, next_billing_renewal
from compute.service import ComputeService
from compute.state import ComputeUnitState
from pydantic import Field
from shared.compute_policy import ComputeUnitRecord, MachinePool, UnitName
from shared.container_requests import schedulable_capacity
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
    DrainSupersededMachine = "drain-superseded-machine"


class WorkerPoolDrainConfig(ContractModel):
    enabled: bool = True
    min_workers: int = 0
    idle_seconds: float = 300


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
    created_at: datetime
    updated_at: datetime

    def request_intake_status(self, *, at: datetime) -> SchedulerWorkerStatus: ...


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

    def has_recoverable_container_request(
        self,
        container_id: str,
        *,
        worker_id: str = "",
    ) -> bool: ...


@runtime_checkable
class WorkerPoolDrainContainerRepository(Protocol):
    def list_by_worker(self, worker_id: str) -> Sequence[WorkerPoolDrainContainer]: ...


class WorkerPoolDrainCapacityOwner(Protocol):
    """The capacity owner's leases, and what is already claimed against it.

    Both leases are taken here and re-entered by whatever this decision calls: a
    surge scales the unit, and scaling takes the same owner's leases for itself.
    """

    def mutation_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...

    def dispatch_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...

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
            with (
                self.capacity_owners.mutation_lock(controller.capacity_owner_id),
                self.capacity_owners.dispatch_lock(controller.capacity_owner_id),
            ):
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
        operational_desired, _maximum = provider_unit_operational_capacity(current_unit)
        if observation.snapshot.observed_machines > operational_desired:
            workers_by_machine = _workers_by_machine(
                self.workers.list_workers_for_capacity_owner(self.capacity_owner_id)
            )
            candidate = _idle_machine_candidate(
                workers_by_machine,
                self.workers,
                self.containers,
                now=current_time,
                idle_seconds=0,
                retain_machines=operational_desired,
                paid_machine_ids=set(),
                reserve_idle_machines=0,
            )
            if candidate is not None:
                pooled = self.compute.release_internal_unit_machine(
                    self.state.workspace_id, self.capacity_owner_id, candidate.machine_id
                )
                return WorkerPoolDrainResult(
                    capacity_owner_id=self.capacity_owner_id,
                    pool=self.pool,
                    action=WorkerPoolDrainAction.TerminateProviderMachine,
                    machine_id=candidate.machine_id,
                    desired_replicas=pooled.desired_machines,
                    observed_replicas=pooled.observed_machines,
                    reason="released idle provider machine above capacity intent",
                )
        config = _drain_config_from_state(self.state)
        if not config.enabled:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                reason="worker-pool drain disabled",
            )
        idle = self._reconcile_idle_capacity(
            current_unit,
            observation=observation,
            config=config,
            now=current_time,
        )
        if idle.action is not WorkerPoolDrainAction.None_:
            return idle
        replacement = self._reconcile_replacement(
            current_unit,
            snapshot=observation.snapshot,
            now=current_time,
        )
        return replacement if replacement is not None else idle

    def _reconcile_idle_capacity(
        self,
        current_unit: ComputeUnitRecord,
        *,
        observation: WorkerPoolDrainObservation,
        config: WorkerPoolDrainConfig,
        now: datetime,
    ) -> WorkerPoolDrainResult:
        current_time = now
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
        if self.state.active_machines <= config.min_workers or (
            current_unit.replacement_machine_id
            and current_unit.desired_machines <= config.min_workers
        ):
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
        machines_by_instance = self.compute.internal_unit_machine_by_instance(
            self.state.workspace_id,
            self.capacity_owner_id,
        )
        paid_machines = {
            machines_by_instance[instance.provider_instance_id]
            for instance in observation.snapshot.instances
            if instance.provider_instance_id in machines_by_instance
            and instance.billing_started_at is not None
            and instance.billing_minimum_seconds is not None
            and instance.billing_quantum_seconds is not None
            and (
                renewal := next_billing_renewal(
                    started_at=instance.billing_started_at,
                    minimum_seconds=instance.billing_minimum_seconds,
                    quantum_seconds=instance.billing_quantum_seconds,
                    now=current_time,
                )
            )
            is not None
            and renewal - current_time > timedelta(seconds=60)
        }
        candidate = _idle_machine_candidate(
            workers_by_machine,
            self.workers,
            self.containers,
            now=current_time,
            idle_seconds=config.idle_seconds,
            retain_machines=config.min_workers,
            paid_machine_ids=paid_machines,
            replacement_machine_id=current_unit.replacement_machine_id,
            reserve_idle_machines=(
                max(
                    ceil(
                        current_unit.min_free_cpu_millicores
                        / schedulable_capacity(current_unit.worker_cpu_millicores)
                    ),
                    ceil(
                        current_unit.min_free_memory_mib
                        / schedulable_capacity(current_unit.worker_memory_mib)
                    ),
                )
                if current_unit.platform_fleet
                and current_unit.worker_cpu_millicores > 0
                and current_unit.worker_memory_mib > 0
                else 0
            ),
        )
        if candidate is None:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                desired_replicas=self.state.desired_machines,
                observed_replicas=self.state.active_machines,
                reason="no idle provider machine candidate",
            )
        if (
            current_unit.desired_machines == 1
            and current_unit.replacement_machine_id
            and config.min_workers == 0
            and current_unit.min_free_cpu_millicores == 0
            and current_unit.min_free_memory_mib == 0
            and current_unit.min_free_gpu_count == 0
            and not paid_machines
            and all(
                not _pool_has_active_containers(workers, self.workers, self.containers)
                and len(_idle_workers(workers, current_time, config.idle_seconds)) == len(workers)
                for workers in workers_by_machine.values()
            )
        ):
            pooled = self.compute.scale_internal_unit(
                self.state.workspace_id,
                self.capacity_owner_id,
                0,
                before_mutation=lambda _unit: None,
                now=current_time,
            )
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                action=WorkerPoolDrainAction.ScaleWorkerPool,
                desired_replicas=pooled.desired_machines,
                observed_replicas=pooled.observed_machines,
                reason="released idle capacity and its replacement surge",
            )
        pooled = self.compute.release_internal_unit_machine(
            self.state.workspace_id,
            self.capacity_owner_id,
            candidate.machine_id,
        )
        desired_replicas = pooled.desired_machines
        observed_replicas = pooled.observed_machines
        reason = "released idle provider machine"
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
        unit: ComputeUnitRecord,
        *,
        snapshot: ProviderUnitSnapshot,
        now: datetime,
    ) -> WorkerPoolDrainResult | None:
        """Replace retiring machines one at a time while preserving the warm floor."""
        current_version = snapshot.current_template_version
        machines_by_instance = self.compute.internal_unit_machine_by_instance(
            self.state.workspace_id,
            self.capacity_owner_id,
        )
        snapshot_machine_ids = {
            machine_id
            for instance in snapshot.instances
            if (machine_id := machines_by_instance.get(instance.provider_instance_id))
        }
        interrupted = {
            machine_id: deadline
            for machine_id, deadline in self.compute.internal_unit_interrupted_machines(
                self.state.workspace_id, self.capacity_owner_id
            ).items()
            if machine_id in snapshot_machine_ids
        }
        superseded = sorted(interrupted, key=interrupted.__getitem__) + [
            machine_id
            for instance in snapshot.instances
            if current_version
            and instance.booted_template_version
            and instance.booted_template_version != current_version
            and (machine_id := machines_by_instance.get(instance.provider_instance_id))
            and machine_id not in interrupted
        ]
        replacement_machine_id = unit.replacement_machine_id
        if replacement_machine_id and replacement_machine_id not in snapshot_machine_ids:
            return self._settle_replacement(
                unit,
                snapshot=snapshot,
                now=now,
                reason="cleared a replacement whose superseded machine is gone",
            )
        if not superseded:
            if replacement_machine_id:
                return self._settle_replacement(
                    unit,
                    snapshot=snapshot,
                    now=now,
                    reason="cleared a replacement after the pool converged",
                )
            return None

        workers_by_machine = _workers_by_machine(
            self.workers.list_workers_for_capacity_owner(self.capacity_owner_id)
        )
        draining_since = self.compute.internal_unit_draining_machines(
            self.state.workspace_id,
            self.capacity_owner_id,
        )
        draining = [
            machine_id
            for machine_id in superseded
            if machine_id in draining_since and machine_id not in interrupted
        ]
        if draining:
            return self._release_draining(
                draining[0],
                workers_by_machine,
            )

        if replacement_machine_id and replacement_machine_id not in superseded:
            return self._settle_replacement(
                unit,
                snapshot=snapshot,
                now=now,
                reason="cleared a replacement whose original machine is no longer superseded",
            )

        # One at a time. The durable pair is what distinguishes the operational
        # surge from autoscaled capacity and keeps every release at the logical
        # desired count.
        sizing_state = self.compute.pool_sizing_snapshot(self.capacity_owner_id)
        if sizing_state.pending_operation_id:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                reason="replacement is waiting on a capacity operation",
            )
        if not replacement_machine_id:
            return self._surge_for_replacement(
                superseded[0],
                current_version,
                logical_desired=unit.desired_machines,
            )

        operational_desired = unit.desired_machines + 1
        if snapshot.desired_machines < operational_desired:
            self.compute.scale_internal_unit(
                self.state.workspace_id,
                self.capacity_owner_id,
                unit.desired_machines,
                before_mutation=lambda _unit: None,
                now=now,
            )
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                action=WorkerPoolDrainAction.SurgeReplacementMachine,
                machine_id=replacement_machine_id,
                desired_replicas=operational_desired,
                observed_replicas=self.state.active_machines,
                reason="restored replacement capacity",
            )
        if (
            snapshot.desired_machines != operational_desired
            or snapshot.observed_machines < operational_desired
            or self.state.active_machines < operational_desired
        ):
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                machine_id=replacement_machine_id,
                desired_replicas=operational_desired,
                observed_replicas=self.state.active_machines,
                reason="replacement machine has not registered",
            )

        accepted_replacement_versions = {
            current_version,
            unit.replacement_template_version,
        }
        registered_replacement = any(
            (
                replacement_machine_id in interrupted
                or instance.booted_template_version in accepted_replacement_versions
            )
            and bool(
                (machine_id := machines_by_instance.get(instance.provider_instance_id))
                and machine_id != replacement_machine_id
                and machine_id not in interrupted
                and (
                    replacement_machine_id not in interrupted
                    or any(
                        worker.request_intake_status(at=now) is SchedulerWorkerStatus.Available
                        for worker in workers_by_machine.get(machine_id, [])
                    )
                )
            )
            for instance in snapshot.instances
        )
        if not registered_replacement:
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                machine_id=replacement_machine_id,
                reason="replacement provider machine has not enrolled",
            )
        if replacement_machine_id in interrupted:
            return self._release_draining(replacement_machine_id, workers_by_machine)
        return self._drain_superseded(
            replacement_machine_id,
            snapshot,
            current_version,
            now=now,
        )

    def _settle_replacement(
        self,
        unit: ComputeUnitRecord,
        *,
        snapshot: ProviderUnitSnapshot,
        now: datetime,
        reason: str,
    ) -> WorkerPoolDrainResult:
        machine_id = unit.replacement_machine_id
        self.compute.clear_internal_unit_replacement(
            self.state.workspace_id,
            self.capacity_owner_id,
            machine_id,
        )
        self.compute.scale_internal_unit(
            self.state.workspace_id,
            self.capacity_owner_id,
            unit.desired_machines,
            before_mutation=lambda _unit: None,
            now=now,
        )
        return WorkerPoolDrainResult(
            capacity_owner_id=self.capacity_owner_id,
            pool=self.pool,
            action=WorkerPoolDrainAction.ScaleWorkerPool,
            machine_id=machine_id,
            desired_replicas=unit.desired_machines,
            observed_replicas=snapshot.observed_machines,
            reason=reason,
        )

    def _surge_for_replacement(
        self,
        machine_id: str,
        current_version: str,
        *,
        logical_desired: int,
    ) -> WorkerPoolDrainResult:
        """Pair and add one replacement without changing logical desired capacity."""
        self.compute.begin_internal_unit_replacement(
            self.state.workspace_id,
            self.capacity_owner_id,
            machine_id,
            template_version=current_version,
        )
        self.compute.scale_internal_unit(
            self.state.workspace_id,
            self.capacity_owner_id,
            logical_desired,
            before_mutation=lambda _unit: None,
        )
        target = logical_desired + 1
        return WorkerPoolDrainResult(
            capacity_owner_id=self.capacity_owner_id,
            pool=self.pool,
            action=WorkerPoolDrainAction.SurgeReplacementMachine,
            machine_id=machine_id,
            desired_replicas=target,
            observed_replicas=self.state.active_machines,
            reason="surged replacement capacity",
        )

    def _drain_superseded(
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
        changed = self.compute.drain_internal_unit_machine(
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
            action=WorkerPoolDrainAction.DrainSupersededMachine,
            machine_id=machine_id,
            desired_replicas=self.state.desired_machines,
            observed_replicas=self.state.active_machines,
            reason=f"draining template {booted}, superseded by {current_version}",
        )

    def _release_draining(
        self,
        machine_id: str,
        workers_by_machine: dict[str, list[WorkerPoolDrainWorker]],
    ) -> WorkerPoolDrainResult | None:
        """Take a draining machine away only after its live work has finished."""
        workers = workers_by_machine.get(machine_id, [])
        if _pool_has_active_containers(workers, self.workers, self.containers):
            return WorkerPoolDrainResult(
                capacity_owner_id=self.capacity_owner_id,
                pool=self.pool,
                machine_id=machine_id,
                reason="machine still has running workloads",
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
    compute_states: Sequence[ComputeUnitState],
    workers: WorkerPoolDrainWorkerRepository,
    containers: WorkerPoolDrainContainerRepository,
) -> list[ManagedComputeWorkerPoolDrainController]:
    controllers: list[ManagedComputeWorkerPoolDrainController] = []
    for state in compute_states:
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
    if not isinstance(raw, str | int | float):
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
    requests: WorkerPoolDrainWorkerRepository,
    containers: WorkerPoolDrainContainerRepository,
) -> bool:
    return any(
        (
            container.status
            in {
                SchedulerContainerStatus.Running,
                SchedulerContainerStatus.Stopping,
            }
            or (
                container.status is SchedulerContainerStatus.Pending
                and not requests.has_recoverable_container_request(
                    container.container_id,
                    worker_id=worker.worker_id,
                )
            )
        )
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
    requests: WorkerPoolDrainWorkerRepository,
    containers: WorkerPoolDrainContainerRepository,
    *,
    now: datetime,
    idle_seconds: float,
    retain_machines: int,
    paid_machine_ids: set[str],
    reserve_idle_machines: int,
    replacement_machine_id: str = "",
) -> _IdleMachineCandidate | None:
    healthy_machines = [
        (machine_id, workers)
        for machine_id, workers in workers_by_machine.items()
        if any(
            worker.request_intake_status(at=now) is SchedulerWorkerStatus.Available
            for worker in workers
        )
    ]
    healthy_machines.sort(
        key=lambda item: (
            min(worker.created_at for worker in item[1]),
            item[0],
        )
    )
    retained_machine_ids = {
        machine_id for machine_id, _workers in healthy_machines[:retain_machines]
    }
    candidates: list[_IdleMachineCandidate] = []
    idle_count = 0
    for machine_id, workers in workers_by_machine.items():
        if _pool_has_active_containers(workers, requests, containers):
            continue
        idle_workers = _idle_workers(workers, now, idle_seconds)
        if len(idle_workers) != len(workers):
            continue
        idle_count += 1
        if machine_id in retained_machine_ids or machine_id in paid_machine_ids:
            continue
        candidates.append(_IdleMachineCandidate(machine_id=machine_id, workers=idle_workers))
    if not candidates or idle_count <= reserve_idle_machines:
        return None
    # Retiring the paired machine settles its surge without lowering demand.
    # Otherwise retire the newest idle machine to preserve the older image cache.
    return max(
        candidates,
        key=lambda item: (
            item.machine_id != replacement_machine_id,
            min(worker.created_at for worker in item.workers),
            item.machine_id,
        ),
    )


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
