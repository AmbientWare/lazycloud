from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from database.tables.capacity_recovery import CapacityRecoveryTable
from database.tables.compute import ComputeCapacityOperationTable, ComputeProviderInstanceTable
from shared.contracts import ContractModel
from shared.timestamps import to_utc
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session


class CapacityRecoveryRecord(ContractModel):
    id: str
    workspace_id: str
    source_unit_id: str
    source_machine_id: str
    observed_at: datetime
    deadline: datetime | None = None
    source_adjusted: bool = False
    source_applied: bool = False
    target_unit_id: str | None = None
    operation_id: str | None = None
    replacement_machine_id: str | None = None
    attempt: int = 0
    next_action_at: datetime
    completed_at: datetime | None = None
    reason: str = ""


def _record(row: CapacityRecoveryTable) -> CapacityRecoveryRecord:
    return CapacityRecoveryRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        source_unit_id=row.source_unit_id,
        source_machine_id=row.source_machine_id,
        observed_at=to_utc(row.observed_at),
        deadline=to_utc(row.deadline) if row.deadline else None,
        source_adjusted=row.source_adjusted,
        source_applied=row.source_applied,
        target_unit_id=row.target_unit_id,
        operation_id=row.operation_id,
        replacement_machine_id=row.replacement_machine_id,
        attempt=row.attempt,
        next_action_at=to_utc(row.next_action_at),
        completed_at=to_utc(row.completed_at) if row.completed_at else None,
        reason=row.reason,
    )


@dataclass(slots=True)
class CapacityRecoveryRepository:
    session: Session

    def delete_completed_for_units(self, unit_ids: Sequence[str]) -> None:
        self.session.execute(
            delete(CapacityRecoveryTable).where(
                CapacityRecoveryTable.completed_at.is_not(None),
                CapacityRecoveryTable.source_unit_id.in_(unit_ids),
                or_(
                    CapacityRecoveryTable.target_unit_id.is_(None),
                    CapacityRecoveryTable.target_unit_id.in_(unit_ids),
                ),
            )
        )

    def record_signal(
        self,
        *,
        workspace_id: str,
        source_unit_id: str,
        source_machine_id: str,
        observed_at: datetime,
        deadline: datetime | None,
        now: datetime,
    ) -> None:
        identity = str(uuid5(NAMESPACE_URL, f"lazycloud:capacity-recovery:{source_machine_id}"))
        self.session.execute(
            insert(CapacityRecoveryTable)
            .values(
                id=identity,
                workspace_id=workspace_id,
                source_unit_id=source_unit_id,
                source_machine_id=source_machine_id,
                observed_at=observed_at,
                deadline=deadline,
                next_action_at=now,
            )
            .on_conflict_do_nothing(index_elements=["source_machine_id"])
        )
        row = self.session.scalar(
            select(CapacityRecoveryTable)
            .where(CapacityRecoveryTable.source_machine_id == source_machine_id)
            .with_for_update()
        )
        if (
            row is not None
            and row.completed_at is None
            and deadline is not None
            and (row.deadline is None or deadline < to_utc(row.deadline))
        ):
            row.deadline = deadline
            row.next_action_at = now
        self.session.flush()

    def get(self, identity: str, *, for_update: bool = False) -> CapacityRecoveryRecord | None:
        row = self.session.get(CapacityRecoveryTable, identity, with_for_update=for_update)
        return _record(row) if row else None

    def save(self, record: CapacityRecoveryRecord) -> None:
        row = self.session.get(CapacityRecoveryTable, record.id, with_for_update=True)
        if row is None or row.completed_at is not None:
            return
        row.source_adjusted = record.source_adjusted
        row.source_applied = record.source_applied
        row.target_unit_id = record.target_unit_id
        row.operation_id = record.operation_id
        row.replacement_machine_id = record.replacement_machine_id
        row.attempt = record.attempt
        row.next_action_at = record.next_action_at
        row.completed_at = record.completed_at
        row.reason = record.reason
        self.session.flush()

    def claim_due(self, *, now: datetime, limit: int) -> list[str]:
        rows = self.session.scalars(
            select(CapacityRecoveryTable)
            .where(
                CapacityRecoveryTable.completed_at.is_(None),
                CapacityRecoveryTable.next_action_at <= now,
            )
            .order_by(
                CapacityRecoveryTable.deadline.asc().nulls_last(),
                CapacityRecoveryTable.next_action_at,
                CapacityRecoveryTable.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.next_action_at = now + timedelta(seconds=15)
        self.session.flush()
        return [row.id for row in rows]

    def active_source_units(self) -> set[str]:
        return set(
            self.session.scalars(
                select(CapacityRecoveryTable.source_unit_id)
                .where(CapacityRecoveryTable.completed_at.is_(None))
                .distinct()
            )
        )

    def unit_has_active_recovery(self, unit_id: str) -> bool:
        return (
            self.session.scalar(
                select(CapacityRecoveryTable.id)
                .where(
                    CapacityRecoveryTable.completed_at.is_(None),
                    or_(
                        CapacityRecoveryTable.source_unit_id == unit_id,
                        CapacityRecoveryTable.target_unit_id == unit_id,
                    ),
                )
                .limit(1)
            )
            is not None
        )

    def protected_sources(self, unit_id: str) -> set[str]:
        return set(
            self.session.scalars(
                select(CapacityRecoveryTable.source_machine_id).where(
                    CapacityRecoveryTable.source_unit_id == unit_id,
                    CapacityRecoveryTable.completed_at.is_(None),
                )
            )
        )

    def active_operations(self, unit_id: str) -> set[str]:
        return {
            identity
            for identity in self.session.scalars(
                select(CapacityRecoveryTable.operation_id).where(
                    CapacityRecoveryTable.target_unit_id == unit_id,
                    CapacityRecoveryTable.completed_at.is_(None),
                )
            )
            if identity is not None
        }

    def machine_is_replacement(self, machine_id: str) -> bool:
        return (
            self.session.scalar(
                select(CapacityRecoveryTable.id)
                .where(
                    or_(
                        CapacityRecoveryTable.replacement_machine_id == machine_id,
                        exists().where(
                            ComputeCapacityOperationTable.operation_id
                            == CapacityRecoveryTable.operation_id,
                            ComputeCapacityOperationTable.target_machine_id == machine_id,
                            ComputeCapacityOperationTable.status == "fulfilled",
                        ),
                    ),
                )
                .limit(1)
            )
            is not None
        )

    def other_target_claims(self, unit_id: str, recovery_id: str, *, observed_at: datetime) -> int:
        pending = (
            select(func.count())
            .select_from(CapacityRecoveryTable)
            .where(
                CapacityRecoveryTable.target_unit_id == unit_id,
                CapacityRecoveryTable.id != recovery_id,
                CapacityRecoveryTable.completed_at.is_(None),
                CapacityRecoveryTable.replacement_machine_id.is_(None),
            )
            .scalar_subquery()
        )
        fulfilled = (
            select(func.count())
            .select_from(ComputeProviderInstanceTable)
            .where(
                ComputeProviderInstanceTable.pool_id == unit_id,
                ComputeProviderInstanceTable.status == "active",
                ComputeProviderInstanceTable.created_at >= observed_at,
                exists().where(
                    CapacityRecoveryTable.replacement_machine_id
                    == ComputeProviderInstanceTable.machine_id
                ),
            )
            .scalar_subquery()
        )
        return self.session.scalar(select(pending + fulfilled)) or 0

    def source_capacity_adjusted(self, machine_id: str) -> bool:
        return bool(
            self.session.scalar(
                select(CapacityRecoveryTable.source_adjusted).where(
                    CapacityRecoveryTable.source_machine_id == machine_id
                )
            )
        )
