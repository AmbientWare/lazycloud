from __future__ import annotations

from dataclasses import dataclass

from database.repositories.compute import ComputeMachineEnrollmentRepository
from scheduler.preemption import CapacityInterruption
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputeMachineEnrollmentStatus,
)

from database import DatabaseClient


@dataclass(frozen=True, slots=True)
class DatabaseCapacityInterruptionSource:
    database: DatabaseClient

    def list_active_interruptions(self) -> list[CapacityInterruption]:
        with self.database.session() as session:
            records = ComputeMachineEnrollmentRepository(session).records.list_across_workspaces()
        return [
            CapacityInterruption(
                enrollment_id=record.id,
                credential_generation=record.credential_generation,
                workspace_id=record.workspace_id,
                pool=record.pool,
                machine_id=record.machine_id,
                state=record.capacity_state,
                reason=record.capacity_reason,
                observed_at=record.capacity_observed_at,
            )
            for record in records
            if record.status is ComputeMachineEnrollmentStatus.Active
            and record.capacity_state
            in {AgentCapacityState.Preempting, AgentCapacityState.Cordoned}
            and record.capacity_observed_at is not None
        ]


__all__ = ["DatabaseCapacityInterruptionSource"]
