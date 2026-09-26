from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from database.repositories.compute import ComputeProviderInstanceRepository, ComputeUnitRepository
from database.repositories.worker_releases import WorkerReleaseRepository
from shared.compute_policy import ComputeCapacityMode, ComputeUnitRecord
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
        # The lease prevents concurrent mutations; the interval claim prevents
        # every scheduler replica from reading the same fleet after it expires.
        with self.compute._required_capacity_owner_mutations().mutation_lock("release-rollout"):
            with self.compute.context.database.session() as session:
                units = ComputeUnitRepository(session).release_rollout_units(
                    {
                        worker.capacity_owner_id
                        for worker in workers
                        if worker.admitted_release_generation > 0
                        and not release.admits(worker.runtime_image, worker.agent_binary_sha256)
                    }
                )
            for unit in units:
                try:
                    with self.compute._required_capacity_owner_mutations().mutation_lock(
                        unit.capacity_owner_id
                    ):
                        changed = self._reconcile_unit(unit, release, workers, now=now)
                    if changed:
                        self.compute.reconcile_unit_capacity(unit.id, now=now)
                except CapacityReservationLockContendedError:
                    continue
                except CapacityReservationLeaseLostError:
                    raise
                except ConflictError as exc:
                    with self.compute.context.database.session() as session:
                        repository = ComputeUnitRepository(session)
                        current = repository.get(unit.id, for_update=True)
                        if current is not None and current.replacement_reason != exc.message:
                            repository.upsert(
                                current.model_copy(
                                    update={
                                        "replacement_reason": exc.message[:512],
                                    }
                                )
                            )
                except Exception:
                    LOGGER.exception("release rollout could not reconcile pool %s", unit.id)

    def _reconcile_unit(
        self,
        unit: ComputeUnitRecord,
        release: ActiveRelease,
        workers: list[SchedulerWorkerRecord],
        *,
        now: datetime,
    ) -> bool:
        if unit.capacity_mode is not ComputeCapacityMode.Pooled:
            return False
        members = [
            worker for worker in workers if worker.capacity_owner_id == unit.capacity_owner_id
        ]
        if unit.replacement_machine_id:
            if not unit.replacement_release_generation:
                return False
            if unit.replacement_release_generation > release.generation:
                return False
            source = next(
                (worker for worker in members if worker.machine_id == unit.replacement_machine_id),
                None,
            )
            with self.compute.context.database.session() as session:
                instance = ComputeProviderInstanceRepository(session).get_by_machine(
                    unit.replacement_machine_id
                )
                updating = WorkerReleaseRepository(session).machine_has_update(
                    unit.replacement_machine_id
                )
            gone = instance is None or instance.status in {"deleted", "failed", "terminating"}
            complete = (
                source is not None
                and release.admits(source.runtime_image, source.agent_binary_sha256)
                and source.request_intake_status(at=now) is SchedulerWorkerStatus.Available
                and not updating
            )
            if gone or complete:
                self.compute.clear_internal_unit_replacement(
                    unit.workspace_id, unit.capacity_owner_id, unit.replacement_machine_id
                )
                return True
            reason = (
                "source agent is offline"
                if source is None
                else "draining workloads or updating the source"
                if updating
                else "replacement accepts work; source update can start"
                if release_replacement(source, workers, release, now=now) is not None
                else "waiting for replacement capacity on the target release"
            )
            self._record_progress(unit, release, reason)
            return False
        for source in members:
            if (
                not source.machine_id
                or source.admitted_release_generation == 0
                or source.status is SchedulerWorkerStatus.Draining
                or release.admits(source.runtime_image, source.agent_binary_sha256)
            ):
                continue
            if release_replacement(source, workers, release, now=now) is not None:
                continue
            with self.compute.context.database.session() as session:
                instance = ComputeProviderInstanceRepository(session).get_by_machine(
                    source.machine_id
                )
            if instance is None or instance.status not in {"active", "resuming"}:
                continue
            self.compute.begin_internal_unit_replacement(
                unit.workspace_id,
                unit.capacity_owner_id,
                source.machine_id,
                release_generation=release.generation,
            )
            return True
        return False

    def _record_progress(
        self, unit: ComputeUnitRecord, release: ActiveRelease, reason: str
    ) -> None:
        with self.compute.context.database.session() as session:
            repository = ComputeUnitRepository(session)
            current = repository.get(unit.id, for_update=True)
            if current is None or current.replacement_machine_id != unit.replacement_machine_id:
                raise ConflictError("release rollout ownership changed")
            if current.replacement_release_generation > release.generation:
                return
            if (
                current.replacement_reason != reason
                or current.replacement_release_generation != release.generation
            ):
                repository.upsert(
                    current.model_copy(
                        update={
                            "replacement_release_generation": release.generation,
                            "replacement_reason": reason,
                        }
                    )
                )
