from __future__ import annotations

from dataclasses import dataclass

from database.tables.compute import (
    ComputeMachineEnrollmentTable,
    ComputeProviderInstanceTable,
    ComputeUnitTable,
)
from database.tables.orchestration import MachineTable, WorkerTable
from shared.compute_enrollment import ComputeMachineEnrollmentStatus
from shared.compute_fleet import MachineLifecycle
from shared.compute_policy import ComputeCapacityMode
from shared.errors import ConflictError
from shared.releases import ActiveRelease
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ReleaseMachineObservation:
    machine_id: str
    worker_id: str
    capacity_owner_id: str
    lifecycle: MachineLifecycle
    provider_status: str
    runtime_image: str
    agent_sha256: str
    update_generation: int
    replacement_reason: str
    update_error: str


@dataclass(slots=True)
class WorkerReleaseRepository:
    session: Session

    def pending_capacity_owners(self) -> list[str]:
        return list(
            self.session.scalars(
                select(ComputeUnitTable.capacity_owner_id).where(
                    ComputeUnitTable.capacity_mode == ComputeCapacityMode.Pooled.value,
                    ComputeUnitTable.phase != "deleted",
                    or_(
                        ComputeUnitTable.replacement_release_generation > 0,
                        ComputeUnitTable.observed_machines
                        != (
                            ComputeUnitTable.desired_machines
                            + ComputeUnitTable.stopped_machines
                            + ComputeUnitTable.retiring_stopped_machines
                        ),
                    ),
                )
            )
        )

    def fleet_observations(self) -> list[ReleaseMachineObservation]:
        machine = MachineTable
        worker = WorkerTable
        instance = ComputeProviderInstanceTable
        enrollment = ComputeMachineEnrollmentTable
        unit = ComputeUnitTable
        rows = self.session.execute(
            select(
                machine.id,
                worker.id,
                unit.capacity_owner_id,
                machine.lifecycle,
                instance.status,
                instance.prepared_worker_image,
                instance.prepared_agent_sha256,
                worker.admitted_runtime_image,
                worker.admitted_agent_sha256,
                worker.update_generation,
                unit.replacement_reason,
                worker.update_error,
            )
            .select_from(machine)
            .outerjoin(worker, worker.machine_id == machine.id)
            .outerjoin(instance, instance.machine_id == machine.id)
            .outerjoin(
                enrollment,
                and_(
                    enrollment.machine_id == machine.id,
                    enrollment.status == ComputeMachineEnrollmentStatus.Active.value,
                ),
            )
            .join(
                unit,
                or_(
                    unit.id == instance.pool_id,
                    unit.capacity_owner_id == enrollment.capacity_owner_id,
                ),
            )
            .where(
                machine.lifecycle.not_in(("deleted", "terminating")),
                or_(machine.lifecycle != "failed", enrollment.id.is_not(None)),
                unit.phase != "deleted",
            )
            .distinct(machine.id)
            .order_by(machine.id, worker.last_seen_at.desc())
        ).tuples()
        return [
            ReleaseMachineObservation(
                machine_id=machine_id,
                worker_id=worker_id or "",
                capacity_owner_id=owner_id,
                lifecycle=MachineLifecycle(lifecycle),
                provider_status=provider_status or "",
                runtime_image=(prepared_image or "")
                if provider_status == "stopped"
                else image or "",
                agent_sha256=(prepared_agent or "")
                if provider_status == "stopped"
                else agent or "",
                update_generation=update_generation or 0,
                replacement_reason=reason,
                update_error=error or "",
            )
            for (
                machine_id,
                worker_id,
                owner_id,
                lifecycle,
                provider_status,
                prepared_image,
                prepared_agent,
                image,
                agent,
                update_generation,
                reason,
                error,
            ) in rows
        ]

    def admit(self, worker: SchedulerWorkerRecord, *, generation: int) -> int:
        row = self._worker(worker.worker_id, worker.machine_id)
        if generation > 0 and generation >= row.admitted_release_generation:
            row.admitted_release_generation = generation
            row.admitted_runtime_image = worker.runtime_image
            row.admitted_agent_sha256 = worker.agent_binary_sha256
            self.session.flush()
        if (
            row.admitted_runtime_image == worker.runtime_image
            and row.admitted_agent_sha256 == worker.agent_binary_sha256
        ):
            return row.admitted_release_generation
        return 0

    def begin_update(self, worker_id: str, machine_id: str, release: ActiveRelease) -> None:
        row = self._worker(worker_id, machine_id)
        if max(row.update_generation, row.admitted_release_generation) > release.generation:
            raise ConflictError("worker update has observed a newer release")
        if row.update_generation == release.generation:
            return
        row.update_generation = release.generation
        row.update_runtime_image = release.target.worker_image
        row.update_agent_sha256 = release.target.agent.sha256 if release.target.agent else ""
        row.update_started_at = utc_now()
        row.update_error = ""
        self.session.flush()

    def record_update_error(
        self, worker_id: str, machine_id: str, *, generation: int, reason: str
    ) -> None:
        row = self._worker(worker_id, machine_id)
        if row.update_generation != generation:
            raise ConflictError("agent update error belongs to a different release")
        row.update_error = reason
        self.session.flush()

    def update_generation(self, worker_id: str, machine_id: str) -> int:
        return self._worker(worker_id, machine_id).update_generation

    def update_blocks_registration(self, worker: SchedulerWorkerRecord) -> bool:
        row = self._worker(worker.worker_id, worker.machine_id)
        return row.update_generation > 0 and (
            worker.runtime_image != row.update_runtime_image
            or bool(
                row.update_agent_sha256 and worker.agent_binary_sha256 != row.update_agent_sha256
            )
        )

    def machine_has_update(self, machine_id: str) -> bool:
        return (
            self.session.scalar(
                select(WorkerTable.id)
                .where(WorkerTable.machine_id == machine_id, WorkerTable.update_generation > 0)
                .limit(1)
            )
            is not None
        )

    def cancel_machine_update(self, machine_id: str) -> None:
        rows = self.session.scalars(
            select(WorkerTable)
            .where(WorkerTable.machine_id == machine_id, WorkerTable.update_generation > 0)
            .with_for_update()
        )
        for row in rows:
            self._clear_update(row)
        self.session.flush()

    def complete_update(self, worker: SchedulerWorkerRecord) -> bool:
        row = self._worker(worker.worker_id, worker.machine_id)
        if not row.update_generation:
            return False
        if (
            worker.runtime_image != row.update_runtime_image
            or (row.update_agent_sha256 and worker.agent_binary_sha256 != row.update_agent_sha256)
            or worker.request_intake_status(at=utc_now()) is not SchedulerWorkerStatus.Available
        ):
            return False
        self._clear_update(row)
        self.session.flush()
        return True

    @staticmethod
    def _clear_update(row: WorkerTable) -> None:
        row.update_generation = 0
        row.update_runtime_image = ""
        row.update_agent_sha256 = ""
        row.update_started_at = None
        row.update_error = ""

    def _worker(self, worker_id: str, machine_id: str) -> WorkerTable:
        row = self.session.scalar(
            select(WorkerTable).where(WorkerTable.id == worker_id).with_for_update()
        )
        if row is None or str(row.machine_id or "") != machine_id:
            raise ConflictError("worker release does not match its enrolled machine")
        return row
