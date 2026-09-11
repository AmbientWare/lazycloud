from __future__ import annotations

from dataclasses import dataclass

from database.tables.orchestration import WorkerTable
from shared.errors import ConflictError
from shared.releases import ActiveRelease
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass(slots=True)
class WorkerReleaseRepository:
    session: Session

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

    def _worker(self, worker_id: str, machine_id: str) -> WorkerTable:
        row = self.session.scalar(
            select(WorkerTable).where(WorkerTable.id == worker_id).with_for_update()
        )
        if row is None or str(row.machine_id or "") != machine_id:
            raise ConflictError("worker release does not match its enrolled machine")
        return row
