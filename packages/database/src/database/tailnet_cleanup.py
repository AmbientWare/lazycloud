from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.compute_enrollment import TailnetCleanupTombstone
from shared.compute_policy import MachinePool

from database.context import ServiceContext
from database.repositories.compute import TailnetCleanupTombstoneRepository


@dataclass(frozen=True, slots=True)
class DatabaseTailnetCleanupStore:
    context: ServiceContext

    def schedule(
        self,
        *,
        workspace_id: str,
        pool: MachinePool,
        machine_id: str,
        generations: list[int],
        auth_key_ids: list[str],
        device_ids: list[str],
        not_before: datetime,
        now: datetime,
    ) -> TailnetCleanupTombstone:
        with self.context.database.session() as session:
            return TailnetCleanupTombstoneRepository(session).schedule(
                workspace_id=workspace_id,
                pool=pool,
                machine_id=machine_id,
                generations=generations,
                auth_key_ids=auth_key_ids,
                device_ids=device_ids,
                not_before=not_before,
                now=now,
            )

    def claim_machine(
        self,
        machine_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> TailnetCleanupTombstone | None:
        with self.context.database.session() as session:
            return TailnetCleanupTombstoneRepository(session).claim_machine(
                machine_id,
                now=now,
                lease_until=lease_until,
            )

    def claim_due(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> list[TailnetCleanupTombstone]:
        with self.context.database.session() as session:
            return TailnetCleanupTombstoneRepository(session).claim_due(
                now=now,
                lease_until=lease_until,
                limit=limit,
            )

    def complete(self, tombstone: TailnetCleanupTombstone) -> bool:
        with self.context.database.session() as session:
            return TailnetCleanupTombstoneRepository(session).complete(tombstone)

    def reschedule(
        self,
        tombstone: TailnetCleanupTombstone,
        *,
        next_attempt_at: datetime,
        last_error: str,
        now: datetime,
    ) -> bool:
        with self.context.database.session() as session:
            return TailnetCleanupTombstoneRepository(session).reschedule(
                tombstone,
                next_attempt_at=next_attempt_at,
                last_error=last_error,
                now=now,
            )

    def get_by_machine(self, machine_id: str) -> TailnetCleanupTombstone | None:
        with self.context.database.session() as session:
            return TailnetCleanupTombstoneRepository(session).get_by_machine(machine_id)

    def pending_count(self) -> int:
        with self.context.database.session() as session:
            return TailnetCleanupTombstoneRepository(session).pending_count()


__all__ = ["DatabaseTailnetCleanupStore"]
