from __future__ import annotations

from dataclasses import dataclass

from database.repositories.compute import ComputeMachineEnrollmentRepository
from scheduler.preemption import CapacityInterruption

from database import DatabaseClient


@dataclass(frozen=True, slots=True)
class DatabaseCapacityInterruptionSource:
    database: DatabaseClient

    def list_active_interruptions(self) -> list[CapacityInterruption]:
        with self.database.session() as session:
            records = ComputeMachineEnrollmentRepository(
                session
            ).list_active_capacity_interruptions()
        return [
            CapacityInterruption(
                enrollment_id=record.enrollment_id,
                credential_generation=record.credential_generation,
                workspace_id=record.workspace_id,
                pool=record.pool,
                machine_id=record.machine_id,
                state=record.state,
                reason=record.reason,
                observed_at=record.observed_at,
            )
            for record in records
        ]


__all__ = ["DatabaseCapacityInterruptionSource"]
