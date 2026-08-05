from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from shared.compute_enrollment import TailnetCleanupTombstone

from networking.tailnet_control import (
    TailnetControlError,
    TailnetIdentityCleanup,
    TailnetMachineIdentityReconciler,
)

DEFAULT_TAILNET_CLEANUP_SETTLE_SECONDS = 30
DEFAULT_TAILNET_CLEANUP_LEASE_SECONDS = 60
DEFAULT_TAILNET_CLEANUP_RETRY_SECONDS = 30
MAX_TAILNET_CLEANUP_RETRY_SECONDS = 900


class TailnetCleanupStore(Protocol):
    def schedule(
        self,
        *,
        workspace_id: str,
        pool_name: str,
        machine_id: str,
        generations: list[int],
        auth_key_ids: list[str],
        device_ids: list[str],
        not_before: datetime,
        now: datetime,
    ) -> TailnetCleanupTombstone: ...

    def claim_machine(
        self,
        machine_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> TailnetCleanupTombstone | None: ...

    def claim_due(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> list[TailnetCleanupTombstone]: ...

    def complete(self, tombstone: TailnetCleanupTombstone) -> bool: ...

    def reschedule(
        self,
        tombstone: TailnetCleanupTombstone,
        *,
        next_attempt_at: datetime,
        last_error: str,
        now: datetime,
    ) -> bool: ...

    def pending_count(self) -> int: ...


@dataclass(frozen=True, slots=True)
class TailnetCleanupAttempt:
    machine_id: str
    completed: bool = False
    rescheduled: bool = False
    removed_device_count: int = 0
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class TailnetCleanupBatch:
    attempts: tuple[TailnetCleanupAttempt, ...] = ()

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
class TailnetCleanupCoordinator:
    store: TailnetCleanupStore
    control: TailnetIdentityCleanup
    settle_seconds: int = DEFAULT_TAILNET_CLEANUP_SETTLE_SECONDS
    lease_seconds: int = DEFAULT_TAILNET_CLEANUP_LEASE_SECONDS
    retry_seconds: int = DEFAULT_TAILNET_CLEANUP_RETRY_SECONDS

    def defer_machine_cleanup(
        self,
        *,
        workspace_id: str,
        pool_name: str,
        machine_id: str,
        generations: tuple[int, ...],
        auth_key_ids: tuple[str, ...],
        device_ids: tuple[str, ...],
        auth_key_expires_at: datetime | None,
        now: datetime | None = None,
    ) -> TailnetCleanupAttempt | None:
        if not generations and not auth_key_ids and not device_ids:
            return None
        current = _as_utc(now or datetime.now(UTC))
        key_expiry = _as_utc(auth_key_expires_at) if auth_key_expires_at is not None else current
        not_before = max(current, key_expiry) + timedelta(seconds=max(self.settle_seconds, 1))
        self.store.schedule(
            workspace_id=workspace_id,
            pool_name=pool_name,
            machine_id=machine_id,
            generations=list(generations),
            auth_key_ids=list(auth_key_ids),
            device_ids=list(device_ids),
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
    ) -> TailnetCleanupBatch:
        current = _as_utc(now or datetime.now(UTC))
        claimed = self.store.claim_due(
            now=current,
            lease_until=current + timedelta(seconds=max(self.lease_seconds, 1)),
            limit=max(limit, 1),
        )
        return TailnetCleanupBatch(
            attempts=tuple(self._reconcile(tombstone, now=current) for tombstone in claimed)
        )

    def _reconcile(
        self,
        tombstone: TailnetCleanupTombstone,
        *,
        now: datetime,
    ) -> TailnetCleanupAttempt:
        try:
            result = TailnetMachineIdentityReconciler(self.control).cleanup(
                machine_id=tombstone.machine_id,
                generations=tuple(tombstone.generations),
                auth_key_ids=tuple(tombstone.auth_key_ids),
                device_ids=tuple(tombstone.device_ids),
            )
        except TailnetControlError as exc:
            retry_at = now + timedelta(seconds=self._retry_delay(tombstone.attempt_count))
            rescheduled = self.store.reschedule(
                tombstone,
                next_attempt_at=retry_at,
                last_error=exc.code.value,
                now=now,
            )
            return TailnetCleanupAttempt(
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
            return TailnetCleanupAttempt(
                machine_id=tombstone.machine_id,
                rescheduled=rescheduled,
                removed_device_count=len(result.removed_device_ids),
            )

        completed = self.store.complete(tombstone)
        return TailnetCleanupAttempt(
            machine_id=tombstone.machine_id,
            completed=completed,
            removed_device_count=len(result.removed_device_ids),
        )

    def _retry_delay(self, attempt_count: int) -> int:
        multiplier = 1 << min(max(attempt_count, 0), 5)
        return min(max(self.retry_seconds, 1) * multiplier, MAX_TAILNET_CLEANUP_RETRY_SECONDS)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("tailnet cleanup timestamp must include a timezone")
    return value.astimezone(UTC)


__all__ = [
    "TailnetCleanupAttempt",
    "TailnetCleanupBatch",
    "TailnetCleanupCoordinator",
    "TailnetCleanupStore",
]
