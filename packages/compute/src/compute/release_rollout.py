from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from database.repositories.capacity_maintenance import CapacityMaintenanceRepository
from database.repositories.compute import ComputeProviderInstanceRepository, ComputeUnitRepository
from database.repositories.worker_releases import WorkerReleaseRepository
from shared.capacity_maintenance import (
    CapacityMaintenanceKind,
    CapacityMaintenancePhase,
    CapacityMaintenanceRecord,
)
from shared.compute_policy import (
    ENDED_UNIT_PHASES,
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitRecord,
    ComputeUnitVisibility,
)
from shared.errors import ConflictError
from shared.releases import ActiveRelease
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus

from compute.capacity_errors import (
    CapacityReservationLeaseLostError,
    CapacityReservationLockContendedError,
)
from compute.maintenance import CapacityMaintenanceService
from compute.pool_provider import PoolProviderService
from compute.unit_reconciliation import UnitReconciliationService

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ComputeReleaseRolloutService:
    maintenance: CapacityMaintenanceService
    providers: PoolProviderService
    reconciliation: UnitReconciliationService

    def reconcile(
        self, release: ActiveRelease, workers: list[SchedulerWorkerRecord], *, now: datetime
    ) -> None:
        with self.providers.required_capacity_owner_mutations().mutation_lock("release-rollout"):
            with self.providers.context.database.session() as session:
                unit_ids = ComputeUnitRepository(session).release_rollout_unit_ids(
                    {
                        worker.capacity_owner_id
                        for worker in workers
                        if worker.admitted_release_generation > 0
                        and not release.admits(worker.runtime_image, worker.agent_binary_sha256)
                    }
                )
            for unit_id in unit_ids:
                try:
                    with self.providers.required_capacity_owner_mutations().mutation_lock(unit_id):
                        with self.providers.context.database.session() as session:
                            unit = ComputeUnitRepository(session).get(unit_id)
                        if unit is None:
                            continue
                        changed = self._reconcile_unit(unit, release, workers, now=now)
                    if (
                        changed or unit.maintenance_active
                    ) and unit.capacity_mode is ComputeCapacityMode.Pooled:
                        self.reconciliation.reconcile_unit_capacity(unit_id, now=now)
                except CapacityReservationLockContendedError:
                    continue
                except CapacityReservationLeaseLostError:
                    raise
                except ConflictError as exc:
                    LOGGER.info("release rollout deferred for %s: %s", unit_id, exc.message)
                except Exception:
                    LOGGER.exception("release rollout could not reconcile pool %s", unit_id)

    def _reconcile_unit(
        self,
        unit: ComputeUnitRecord,
        release: ActiveRelease,
        workers: list[SchedulerWorkerRecord],
        *,
        now: datetime,
    ) -> bool:
        if unit.phase is ComputeUnitPhase.Deleting:
            return False
        with self.providers.context.database.session() as session:
            repository = CapacityMaintenanceRepository(session)
            active = repository.active_for_pools([unit.id])
            if unit.phase is ComputeUnitPhase.Deleted:
                # Provider reconciliation proves instance and storage absence
                # before the unit reaches Deleted.
                for operation in active:
                    repository.transition(
                        operation.id,
                        expected_generation=operation.release_generation,
                        expected_phase=operation.phase,
                        phase=CapacityMaintenancePhase.Complete,
                        now=now,
                    )
                return False
        changed = False
        members = {
            worker.machine_id: worker
            for worker in workers
            if worker.capacity_owner_id == unit.capacity_owner_id
        }
        for operation in active:
            changed = self._advance(unit, operation, release, members, now=now) or changed
        if (
            unit.capacity_mode is not ComputeCapacityMode.Pooled
            or unit.visibility is not ComputeUnitVisibility.Internal
            or unit.phase in ENDED_UNIT_PHASES
        ):
            return changed
        active_sources = {operation.source_machine_id for operation in active}
        for source in members.values():
            if (
                not source.machine_id
                or source.admitted_release_generation == 0
                or release.admits(source.runtime_image, source.agent_binary_sha256)
            ):
                continue
            try:
                operation = self.maintenance.prepare_worker_release(source, release, workers)
                changed = (
                    source.machine_id not in active_sources and operation.surge_machines > 0
                ) or changed
            except ConflictError as exc:
                LOGGER.debug("worker %s update waits: %s", source.worker_id, exc.message)
        return changed

    def _advance(
        self,
        unit: ComputeUnitRecord,
        operation: CapacityMaintenanceRecord,
        release: ActiveRelease,
        workers: dict[str, SchedulerWorkerRecord],
        *,
        now: datetime,
    ) -> bool:
        if operation.release_generation > release.generation:
            return False
        with self.providers.context.database.session() as session:
            instances = ComputeProviderInstanceRepository(session)
            source = instances.get_by_machine(operation.source_machine_id)
            updating = WorkerReleaseRepository(session).machine_has_update(
                operation.source_machine_id
            )
            repository = CapacityMaintenanceRepository(session)
            worker = workers.get(operation.source_machine_id)
            current = (
                worker is not None
                and release.admits(worker.runtime_image, worker.agent_binary_sha256)
                and worker.request_intake_status(at=now) is SchedulerWorkerStatus.Available
                and not updating
            )
            if operation.kind is CapacityMaintenanceKind.ReserveRefresh:
                if (
                    source is None
                    or source.status == "deleted"
                    or (
                        source.status == "stopped"
                        and release.admits(
                            source.prepared_worker_image, source.prepared_agent_sha256
                        )
                    )
                ):
                    if operation.release_generation < release.generation:
                        operation = repository.retarget(
                            operation.id,
                            expected_generation=operation.release_generation,
                            release_generation=release.generation,
                            now=now,
                        )
                    repository.transition(
                        operation.id,
                        expected_generation=operation.release_generation,
                        expected_phase=operation.phase,
                        phase=CapacityMaintenancePhase.Complete,
                        now=now,
                    )
                elif operation.phase is not CapacityMaintenancePhase.Failed:
                    reason = (
                        "waiting for provider instance and storage cleanup"
                        if source.status == "terminating" or source.missing_since is not None
                        else "waiting for the prepared reserve to stop"
                        if source.status == "stopping"
                        else "waiting for the reserve to boot and prepare the target release"
                    )
                    if reason != operation.reason:
                        repository.transition(
                            operation.id,
                            expected_generation=operation.release_generation,
                            expected_phase=operation.phase,
                            phase=operation.phase,
                            reason=reason,
                            now=now,
                        )
                return False
            if operation.phase is CapacityMaintenancePhase.Retiring:
                records = instances.list_for_pool(unit.id, excluded_statuses=("deleted",))
                physical = sum(record.instance_id is not None for record in records)
                wanted = (
                    unit.desired_machines
                    + unit.stopped_machines
                    + unit.maintenance_surge_machines
                    + int(bool(unit.replacement_machine_id))
                )
                if physical <= wanted:
                    repository.transition(
                        operation.id,
                        expected_generation=operation.release_generation,
                        expected_phase=operation.phase,
                        phase=CapacityMaintenancePhase.Complete,
                        now=now,
                    )
                elif operation.reason != "waiting for provider instance and storage cleanup":
                    repository.transition(
                        operation.id,
                        expected_generation=operation.release_generation,
                        expected_phase=operation.phase,
                        phase=operation.phase,
                        reason="waiting for provider instance and storage cleanup",
                        now=now,
                    )
                return False
            if operation.release_generation < release.generation:
                operation = repository.retarget(
                    operation.id,
                    expected_generation=operation.release_generation,
                    release_generation=release.generation,
                    now=now,
                )
            gone = unit.capacity_mode is ComputeCapacityMode.Pooled and (
                source is None or source.status == "deleted"
            )
            if current or gone:
                phase = (
                    CapacityMaintenancePhase.Retiring
                    if operation.surge_machines
                    else CapacityMaintenancePhase.Complete
                )
                repository.transition(
                    operation.id,
                    expected_generation=operation.release_generation,
                    expected_phase=operation.phase,
                    phase=phase,
                    now=now,
                )
                return operation.surge_machines > 0
            reason = (
                "source agent is offline"
                if worker is None
                else "draining workloads or updating the source"
                if updating
                else "waiting for replacement capacity on the target release"
                if operation.replacement_machine_id is None and operation.surge_machines
                else "idle source update can start"
                if operation.replacement_machine_id is None
                else "replacement reserved; source update can start"
            )
            phase = CapacityMaintenancePhase.Draining if updating else operation.phase
            if phase != operation.phase or reason != operation.reason:
                repository.transition(
                    operation.id,
                    expected_generation=operation.release_generation,
                    expected_phase=operation.phase,
                    phase=phase,
                    now=now,
                    reason=reason,
                )
        return False
