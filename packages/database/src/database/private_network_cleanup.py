from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.compute_enrollment import PrivateNetworkCleanupTombstone
from shared.compute_policy import MachinePool

from database.context import ServiceContext
from database.repositories.compute import PrivateNetworkCleanupTombstoneRepository


@dataclass(frozen=True, slots=True)
class DatabasePrivateNetworkCleanupStore:
    context: ServiceContext

    def schedule(
        self,
        *,
        workspace_id: str,
        pool: MachinePool,
        machine_id: str,
        resource_ids: list[str],
        site_ids: list[str],
        not_before: datetime,
        now: datetime,
    ) -> PrivateNetworkCleanupTombstone:
        with self.context.database.session() as session:
            return PrivateNetworkCleanupTombstoneRepository(session).schedule(
                workspace_id=workspace_id,
                pool=pool,
                machine_id=machine_id,
                resource_ids=resource_ids,
                site_ids=site_ids,
                not_before=not_before,
                now=now,
            )

    def claim_machine(
        self,
        machine_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> PrivateNetworkCleanupTombstone | None:
        with self.context.database.session() as session:
            return PrivateNetworkCleanupTombstoneRepository(session).claim_machine(
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
    ) -> list[PrivateNetworkCleanupTombstone]:
        with self.context.database.session() as session:
            return PrivateNetworkCleanupTombstoneRepository(session).claim_due(
                now=now,
                lease_until=lease_until,
                limit=limit,
            )

    def complete(self, tombstone: PrivateNetworkCleanupTombstone) -> bool:
        with self.context.database.session() as session:
            return PrivateNetworkCleanupTombstoneRepository(session).complete(tombstone)

    def reschedule(
        self,
        tombstone: PrivateNetworkCleanupTombstone,
        *,
        next_attempt_at: datetime,
        last_error: str,
        now: datetime,
    ) -> bool:
        with self.context.database.session() as session:
            return PrivateNetworkCleanupTombstoneRepository(session).reschedule(
                tombstone,
                next_attempt_at=next_attempt_at,
                last_error=last_error,
                now=now,
            )

    def get_by_machine(self, machine_id: str) -> PrivateNetworkCleanupTombstone | None:
        with self.context.database.session() as session:
            return PrivateNetworkCleanupTombstoneRepository(session).get_by_machine(machine_id)

    def pending_count(self) -> int:
        with self.context.database.session() as session:
            return PrivateNetworkCleanupTombstoneRepository(session).pending_count()


__all__ = ["DatabasePrivateNetworkCleanupStore"]
