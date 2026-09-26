from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from compute.service import ComputeService

LOGGER = logging.getLogger(__name__)


def release_replacement(
    source: SchedulerWorkerRecord,
    workers: list[SchedulerWorkerRecord],
    release: ActiveRelease,
    *,
    now: datetime,
) -> SchedulerWorkerRecord | None:
    """A current worker in the same pool with room for the source's allocations."""
    for candidate in workers:
        if (
            candidate.machine_id == source.machine_id
            or candidate.capacity_owner_id != source.capacity_owner_id
            or candidate.placement != source.placement
            or candidate.owner_user_id != source.owner_user_id
            or candidate.gpu_type != source.gpu_type
            or candidate.disk_storage != source.disk_storage
            or (
                source.total_disk_volumes > source.free_disk_volumes
                and source.availability_zone
                and candidate.availability_zone != source.availability_zone
            )
            or not set(source.runtime_classes).issubset(candidate.runtime_classes)
            or candidate.request_intake_status(at=now) is not SchedulerWorkerStatus.Available
            or not release.admits(candidate.runtime_image, candidate.agent_binary_sha256)
        ):
            continue
        if all(
            free >= max(total - remaining, 0)
            for free, total, remaining in (
                (
                    candidate.free_cpu_millicores,
                    source.total_cpu_millicores,
                    source.free_cpu_millicores,
                ),
                (candidate.free_memory_mib, source.total_memory_mib, source.free_memory_mib),
                (candidate.free_gpu_count, source.total_gpu_count, source.free_gpu_count),
                (candidate.free_disk_bytes, source.total_disk_bytes, source.free_disk_bytes),
                (candidate.free_disk_volumes, source.total_disk_volumes, source.free_disk_volumes),
            )
        ):
            return candidate
    return None


@dataclass(slots=True)
class ComputeReleaseRolloutService:
    compute: ComputeService

    def reconcile(
        self, release: ActiveRelease, workers: list[SchedulerWorkerRecord], *, now: datetime
    ) -> None:
        with self.compute._required_capacity_owner_mutations().mutation_lock("release-rollout"):
            with self.compute.context.database.session() as session:
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
                    with self.compute._required_capacity_owner_mutations().mutation_lock(unit_id):
                        with self.compute.context.database.session() as session:
                            unit = ComputeUnitRepository(session).get(unit_id)
                        if unit is None:
                            continue
                        changed = self._reconcile_unit(unit, release, workers, now=now)
                    if (
                        changed or unit.maintenance_active
                    ) and unit.capacity_mode is ComputeCapacityMode.Pooled:
                        self.compute.reconcile_unit_capacity(unit_id, now=now)
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
        with self.compute.context.database.session() as session:
            active = CapacityMaintenanceRepository(session).active_for_pools([unit.id])
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
                operation = self.compute.prepare_worker_release(source, release, workers)
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
        with self.compute.context.database.session() as session:
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
