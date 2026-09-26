from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from database.tables.capacity_maintenance import CapacityMaintenanceTable
from database.tables.compute import ComputeProviderInstanceTable, ComputeUnitTable
from database.tables.orchestration import MachineTable
from shared.capacity_maintenance import (
    CapacityMaintenanceCommitments,
    CapacityMaintenanceKind,
    CapacityMaintenancePhase,
    CapacityMaintenanceRecord,
)
from shared.errors import ConflictError
from shared.placement import Placement
from shared.scheduling import SchedulerWorkerRecord
from shared.timestamps import to_utc
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session


def _record(row: CapacityMaintenanceTable) -> CapacityMaintenanceRecord:
    return CapacityMaintenanceRecord(
        id=row.id,
        pool_id=row.pool_id,
        source_machine_id=row.source_machine_id,
        release_generation=row.release_generation,
        kind=CapacityMaintenanceKind(row.kind),
        phase=CapacityMaintenancePhase(row.phase),
        surge_machines=row.surge_machines,
        running_cpu_millicores=row.running_cpu_millicores,
        hourly_cost_micros=row.hourly_cost_micros,
        replacement_machine_id=row.replacement_machine_id,
        reserved_cpu_millicores=row.reserved_cpu_millicores,
        reserved_memory_mib=row.reserved_memory_mib,
        reserved_gpu_count=row.reserved_gpu_count,
        reserved_disk_bytes=row.reserved_disk_bytes,
        reserved_disk_volumes=row.reserved_disk_volumes,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
        completed_at=to_utc(row.completed_at) if row.completed_at else None,
        reason=row.reason,
    )


@dataclass(slots=True)
class CapacityMaintenanceRepository:
    session: Session

    def get(self, identity: str) -> CapacityMaintenanceRecord | None:
        row = self.session.get(CapacityMaintenanceTable, identity)
        return _record(row) if row is not None else None

    def reserve_allocations(
        self,
        operation: CapacityMaintenanceRecord,
        worker: SchedulerWorkerRecord,
    ) -> None:
        if (
            operation.source_machine_id != worker.machine_id
            or operation.pool_id != worker.capacity_owner_id
        ):
            raise ConflictError("maintenance source ownership changed")
        table = CapacityMaintenanceTable
        changed = self.session.scalar(
            update(table)
            .where(
                table.id == operation.id,
                table.release_generation == operation.release_generation,
                table.phase == operation.phase.value,
                table.completed_at.is_(None),
            )
            .values(
                reserved_cpu_millicores=func.greatest(
                    table.reserved_cpu_millicores,
                    worker.total_cpu_millicores - worker.free_cpu_millicores,
                    0,
                ),
                reserved_memory_mib=func.greatest(
                    table.reserved_memory_mib, worker.total_memory_mib - worker.free_memory_mib, 0
                ),
                reserved_gpu_count=func.greatest(
                    table.reserved_gpu_count, worker.total_gpu_count - worker.free_gpu_count, 0
                ),
                reserved_disk_bytes=func.greatest(
                    table.reserved_disk_bytes, worker.total_disk_bytes - worker.free_disk_bytes, 0
                ),
                reserved_disk_volumes=func.greatest(
                    table.reserved_disk_volumes,
                    worker.total_disk_volumes - worker.free_disk_volumes,
                    0,
                ),
            )
            .returning(table.id)
        )
        if changed is None:
            raise ConflictError("maintenance ownership or phase changed")

    def reserved_resources(
        self,
        *,
        placement: Placement,
        preemptible: bool,
        gpu_type: str,
    ) -> tuple[int, int, int]:
        table = CapacityMaintenanceTable
        return (
            self.session.execute(
                select(
                    func.coalesce(func.sum(table.reserved_cpu_millicores), 0),
                    func.coalesce(func.sum(table.reserved_memory_mib), 0),
                    func.coalesce(func.sum(table.reserved_gpu_count), 0),
                )
                .join(ComputeUnitTable, ComputeUnitTable.id == table.pool_id)
                .where(
                    ComputeUnitTable.placement == placement.key,
                    ComputeUnitTable.worker_preemptible == preemptible,
                    ComputeUnitTable.worker_gpu_type == gpu_type,
                    table.completed_at.is_(None),
                    table.phase != "retiring",
                )
            )
            .tuples()
            .one()
        )

    def active_for_pools(self, pool_ids: Collection[str]) -> list[CapacityMaintenanceRecord]:
        table = CapacityMaintenanceTable
        return [
            _record(row)
            for row in self.session.scalars(
                select(table)
                .where(table.pool_id.in_(pool_ids), table.completed_at.is_(None))
                .order_by(table.created_at, table.id)
            )
        ]

    def active_for_placement(self, placement: Placement) -> list[CapacityMaintenanceRecord]:
        table = CapacityMaintenanceTable
        return [
            _record(row)
            for row in self.session.scalars(
                select(table)
                .join(ComputeUnitTable, ComputeUnitTable.id == table.pool_id)
                .where(ComputeUnitTable.placement == placement.key, table.completed_at.is_(None))
                .order_by(table.created_at, table.id)
            )
        ]

    def commitments(self, pool_ids: Collection[str]) -> CapacityMaintenanceCommitments:
        table = CapacityMaintenanceTable
        row = self.session.execute(
            select(
                func.count(table.id),
                func.coalesce(func.sum(table.surge_machines), 0),
                func.coalesce(func.sum(table.running_cpu_millicores), 0),
                func.coalesce(func.sum(table.hourly_cost_micros), 0),
                func.count(table.id).filter(table.hourly_cost_micros.is_(None)),
            ).where(table.pool_id.in_(pool_ids), table.completed_at.is_(None))
        ).one()
        return CapacityMaintenanceCommitments(
            operations=row[0],
            surge_machines=row[1],
            running_cpu_millicores=row[2],
            hourly_cost_micros=row[3],
            unknown_cost_operations=row[4],
        )

    def surge_by_pool(self, pool_ids: Collection[str]) -> dict[str, int]:
        table = CapacityMaintenanceTable
        return {
            pool_id: surge
            for pool_id, surge in self.session.execute(
                select(table.pool_id, func.sum(table.surge_machines))
                .where(table.pool_id.in_(pool_ids), table.completed_at.is_(None))
                .group_by(table.pool_id)
            ).tuples()
        }

    def start(self, record: CapacityMaintenanceRecord) -> CapacityMaintenanceRecord:
        if record.completed_at is not None or record.phase is not CapacityMaintenancePhase.Planned:
            raise ConflictError("maintenance must start in the planned phase")
        table = CapacityMaintenanceTable
        machine = self.session.scalar(
            select(MachineTable.id)
            .where(MachineTable.id == record.source_machine_id)
            .with_for_update()
        )
        if machine is None:
            raise ConflictError("maintenance source machine is missing")
        latest = self.session.scalar(
            select(table)
            .where(table.source_machine_id == record.source_machine_id)
            .order_by(table.release_generation.desc())
            .limit(1)
        )
        if latest is not None and latest.release_generation > record.release_generation:
            raise ConflictError("maintenance has observed a newer release")
        self.session.execute(
            insert(table).values(**record.model_dump(mode="python")).on_conflict_do_nothing()
        )
        row = self.session.scalar(
            select(table)
            .where(
                table.source_machine_id == record.source_machine_id,
                table.release_generation == record.release_generation,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.release_generation != record.release_generation
            or row.pool_id != record.pool_id
            or row.kind != record.kind.value
            or row.surge_machines != record.surge_machines
            or row.running_cpu_millicores != record.running_cpu_millicores
            or row.hourly_cost_micros != record.hourly_cost_micros
            or (
                record.replacement_machine_id is not None
                and row.replacement_machine_id != record.replacement_machine_id
            )
        ):
            raise ConflictError("maintenance source or replacement is already reserved")
        return _record(row)

    def transition(
        self,
        identity: str,
        *,
        expected_generation: int,
        expected_phase: CapacityMaintenancePhase,
        phase: CapacityMaintenancePhase,
        now: datetime,
        reason: str = "",
        replacement_machine_id: str | None = None,
    ) -> CapacityMaintenanceRecord:
        table = CapacityMaintenanceTable
        statement = (
            update(table)
            .where(
                table.id == identity,
                table.release_generation == expected_generation,
                table.phase == expected_phase.value,
                table.completed_at.is_(None),
            )
            .values(
                phase=phase.value,
                updated_at=now,
                completed_at=now if phase is CapacityMaintenancePhase.Complete else None,
                reason=reason,
            )
        )
        if replacement_machine_id is not None:
            statement = statement.where(
                or_(
                    table.replacement_machine_id.is_(None),
                    table.replacement_machine_id == replacement_machine_id,
                )
            ).values(replacement_machine_id=replacement_machine_id)
        row = self.session.scalar(statement.returning(table))
        if row is None:
            raise ConflictError("maintenance ownership or phase changed")
        return _record(row)

    def release_deleted_replacement(
        self, operation: CapacityMaintenanceRecord, *, now: datetime
    ) -> CapacityMaintenanceRecord:
        table = CapacityMaintenanceTable
        provider = ComputeProviderInstanceTable
        row = self.session.scalar(
            update(table)
            .where(
                table.id == operation.id,
                table.release_generation == operation.release_generation,
                table.phase == operation.phase.value,
                table.completed_at.is_(None),
                table.replacement_machine_id == operation.replacement_machine_id,
                ~select(provider.id)
                .where(
                    provider.machine_id == operation.replacement_machine_id,
                    provider.status != "deleted",
                )
                .exists(),
            )
            .values(replacement_machine_id=None, updated_at=now)
            .returning(table)
        )
        if row is None:
            raise ConflictError("maintenance replacement is still present or ownership changed")
        return _record(row)

    def retarget(
        self,
        identity: str,
        *,
        expected_generation: int,
        release_generation: int,
        now: datetime,
    ) -> CapacityMaintenanceRecord:
        if release_generation <= expected_generation:
            raise ConflictError("maintenance requires a newer release generation")
        table = CapacityMaintenanceTable
        row = self.session.scalar(
            update(table)
            .where(
                table.id == identity,
                table.release_generation == expected_generation,
                table.completed_at.is_(None),
            )
            .values(release_generation=release_generation, updated_at=now)
            .returning(table)
        )
        if row is None:
            raise ConflictError("maintenance ownership changed")
        return _record(row)
