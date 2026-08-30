from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from shared.compute_enrollment import PrivateNetworkCleanupTombstone
from shared.compute_policy import MachinePool

from networking.private_network_control import (
    PrivateNetworkControlError,
    PrivateNetworkIdentityCleanup,
    PrivateNetworkMachineIdentityReconciler,
)

DEFAULT_PRIVATE_NETWORK_CLEANUP_SETTLE_SECONDS = 5
DEFAULT_PRIVATE_NETWORK_CLEANUP_LEASE_SECONDS = 30
DEFAULT_PRIVATE_NETWORK_CLEANUP_RETRY_SECONDS = 5
MAX_PRIVATE_NETWORK_CLEANUP_RETRY_SECONDS = 300


class PrivateNetworkCleanupStore(Protocol):
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
    ) -> PrivateNetworkCleanupTombstone: ...

    def claim_machine(
        self,
        machine_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> PrivateNetworkCleanupTombstone | None: ...

    def claim_due(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> list[PrivateNetworkCleanupTombstone]: ...

    def complete(self, tombstone: PrivateNetworkCleanupTombstone) -> bool: ...

    def reschedule(
        self,
        tombstone: PrivateNetworkCleanupTombstone,
        *,
        next_attempt_at: datetime,
        last_error: str,
        now: datetime,
    ) -> bool: ...

    def pending_count(self) -> int: ...


@dataclass(frozen=True, slots=True)
class PrivateNetworkCleanupAttempt:
    machine_id: str
    completed: bool = False
    rescheduled: bool = False
    removed_identity_count: int = 0
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class PrivateNetworkCleanupBatch:
    attempts: tuple[PrivateNetworkCleanupAttempt, ...] = ()

    @property
    def processed_count(self) -> int:
        return len(self.attempts)

    @property
    def completed_count(self) -> int:
        return sum(attempt.completed for attempt in self.attempts)

    @property
    def failure_count(self) -> int:
        return sum(bool(attempt.error_code) for attempt in self.attempts)


@dataclass(frozen=True, slots=True)
class PrivateNetworkCleanupCoordinator:
    store: PrivateNetworkCleanupStore
    control: PrivateNetworkIdentityCleanup
    settle_seconds: int = DEFAULT_PRIVATE_NETWORK_CLEANUP_SETTLE_SECONDS
    lease_seconds: int = DEFAULT_PRIVATE_NETWORK_CLEANUP_LEASE_SECONDS
    retry_seconds: int = DEFAULT_PRIVATE_NETWORK_CLEANUP_RETRY_SECONDS

    def defer_machine_cleanup(
        self,
        *,
        workspace_id: str,
        pool: MachinePool,
        machine_id: str,
        resource_ids: tuple[str, ...] = (),
        site_ids: tuple[str, ...],
        now: datetime | None = None,
    ) -> PrivateNetworkCleanupAttempt | None:
        if not resource_ids and not site_ids:
            return None
        current = _as_utc(now or datetime.now(UTC))
        not_before = current + timedelta(seconds=max(self.settle_seconds, 1))
        self.store.schedule(
            workspace_id=workspace_id,
            pool=pool,
            machine_id=machine_id,
            resource_ids=list(resource_ids),
            site_ids=list(site_ids),
            not_before=not_before,
            now=current,
        )
        claimed = self.store.claim_machine(
            machine_id,
            now=current,
            lease_until=current + timedelta(seconds=max(self.lease_seconds, 1)),
        )
        return self._reconcile(claimed, now=current) if claimed is not None else None

    def reconcile_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> PrivateNetworkCleanupBatch:
        current = _as_utc(now or datetime.now(UTC))
        claimed = self.store.claim_due(
            now=current,
            lease_until=current + timedelta(seconds=max(self.lease_seconds, 1)),
            limit=max(limit, 1),
        )
        return PrivateNetworkCleanupBatch(
            attempts=tuple(self._reconcile(tombstone, now=current) for tombstone in claimed)
        )

    def _reconcile(
        self,
        tombstone: PrivateNetworkCleanupTombstone,
        *,
        now: datetime,
    ) -> PrivateNetworkCleanupAttempt:
        try:
            removed = PrivateNetworkMachineIdentityReconciler(self.control).cleanup(
                resource_ids=tuple(tombstone.resource_ids),
                site_ids=tuple(tombstone.site_ids),
            )
        except PrivateNetworkControlError as exc:
            retry_at = now + timedelta(seconds=self._retry_delay(tombstone.attempt_count))
            rescheduled = self.store.reschedule(
                tombstone,
                next_attempt_at=retry_at,
                last_error=exc.code.value,
                now=now,
            )
            return PrivateNetworkCleanupAttempt(
                machine_id=tombstone.machine_id,
                rescheduled=rescheduled,
                error_code=exc.code.value,
            )
        if now < tombstone.not_before:
            rescheduled = self.store.reschedule(
                tombstone,
                next_attempt_at=tombstone.not_before,
                last_error="",
                now=now,
            )
            return PrivateNetworkCleanupAttempt(
                machine_id=tombstone.machine_id,
                rescheduled=rescheduled,
                removed_identity_count=removed,
            )
        return PrivateNetworkCleanupAttempt(
            machine_id=tombstone.machine_id,
            completed=self.store.complete(tombstone),
            removed_identity_count=removed,
        )

    def _retry_delay(self, attempt_count: int) -> int:
        multiplier = 1 << min(max(attempt_count, 0), 5)
        return min(
            max(self.retry_seconds, 1) * multiplier,
            MAX_PRIVATE_NETWORK_CLEANUP_RETRY_SECONDS,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("private-network cleanup timestamp must include a timezone")
    return value.astimezone(UTC)


__all__ = [
    "PrivateNetworkCleanupAttempt",
    "PrivateNetworkCleanupBatch",
    "PrivateNetworkCleanupCoordinator",
    "PrivateNetworkCleanupStore",
]
