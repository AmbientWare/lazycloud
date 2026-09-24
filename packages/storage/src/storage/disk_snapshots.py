"""Provider snapshots of disk volumes: taking, completing, superseding and deleting them.

A snapshot is a second copy of a generation the control plane already recorded,
never the only one. Every generation still publishes its chunks to object
storage, so losing a snapshot, or never finishing one, loses nothing. What a
snapshot buys is a start: a new volume made from it holds the disk the moment it
attaches, and the provider loads its blocks as they are first read.

Only the disk's current holder may take one, for the disk's current generation,
of the volume the disk row names. The row is written before the provider is
asked, and a retry for the same generation, account and region returns the
same snapshot. The provider's API takes no client token, so the row's token
rides in the snapshot's tags, where a retry or the sweep finds it.

The sweep follows each snapshot until it completes, then deletes every older
snapshot of the disk except one a volume creation is still reading. Each
snapshot of a volume is incremental on the ones before it and deleting older
ones leaves the newest whole, so a disk keeps one completed snapshot at rest.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from compute.block_volumes import (
    BlockSnapshotRequest,
    BlockSnapshotState,
    BlockVolumeMissingError,
    BlockVolumeOwner,
    BlockVolumePendingError,
    BlockVolumeProvider,
    BlockVolumeProviders,
    BlockVolumeScope,
    orphaned_snapshots,
)
from database.repositories.disk_snapshots import (
    DiskSnapshotRepository,
    DiskSnapshotRow,
    DiskSnapshotScope,
)
from database.repositories.disk_volumes import DiskVolumeRepository
from database.repositories.disks import DiskRepository
from shared.errors import ConflictError, DiskVolumePendingError, NotFoundError
from shared.timestamps import to_utc, utc_now

from database import DatabaseClient
from storage.disk_volumes import DISK_VOLUME_ORPHAN_MIN_AGE_SECONDS
from storage.disks import refuse_stale_lease

LOGGER = logging.getLogger(__name__)

DISK_SNAPSHOT_POLL_SECONDS = 30
"""How often a pending snapshot is asked whether it has completed."""

DISK_SNAPSHOT_CREATE_SILENCE_SECONDS = 5 * 60
"""How long a creation may go unrecorded before the sweep settles it from the tags.

Far past any one CreateSnapshot call, so the sweep never races the holder's own.
"""

DISK_SNAPSHOT_DELETE_RETRY_SECONDS = 60
DISK_SNAPSHOT_ORPHAN_INTERVAL_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class DiskSnapshotTaken:
    taken: bool
    snapshot_id: str = ""
    reason: str = ""
    """Why no snapshot was taken this time; the holder asks again after its next publish."""


@dataclass(slots=True)
class DiskSnapshotService:
    database: DatabaseClient
    providers: BlockVolumeProviders
    deployment: str
    """The platform deployment's namespace, stamped on every snapshot it creates."""

    clock: Callable[[], datetime] = utc_now
    _orphans_due_at: datetime | None = field(default=None, init=False)

    def take(
        self,
        disk_id: str,
        *,
        container_id: str,
        lease_token: str,
        generation: int,
        final: bool,
    ) -> DiskSnapshotTaken:
        """Snapshot the holder's volume at the disk's current generation.

        A periodic request waits while an earlier snapshot of the disk is still
        pending, so a volume never has a queue of them. The holder's last request,
        at release, does not wait, and answers `DiskVolumePendingError` while the
        provider refuses a second snapshot this soon.
        """
        now = self.clock()
        with self.database.session() as session:
            row = DiskRepository(session).lock(disk_id)
            if row is None:
                raise NotFoundError(f"disk not found: {disk_id}")
            if row.deleted_at is not None:
                raise ConflictError(f"disk {row.name} is deleting")
            refuse_stale_lease(row, container_id, lease_token)
            if generation != row.generation:
                raise ConflictError(
                    f"disk {row.name} is at generation {row.generation}; a snapshot of "
                    f"generation {generation} would not be its newest"
                )
            if row.volume_state != "attached" or not row.volume_id:
                raise ConflictError(f"disk {row.name} has no attached volume to snapshot")
            snapshots = DiskSnapshotRepository(session)
            existing = snapshots.for_generation(
                disk_id,
                generation=generation,
                provider_ref=row.volume_provider_ref,
                region=row.volume_region,
            )
            if existing is not None and existing.state in {"pending", "completed"}:
                return DiskSnapshotTaken(taken=True, snapshot_id=existing.snapshot_id)
            if existing is not None and existing.state == "deleting":
                return DiskSnapshotTaken(
                    taken=False, reason=f"generation {generation}'s snapshot is being deleted"
                )
            if existing is None:
                if not final and snapshots.in_flight(disk_id):
                    return DiskSnapshotTaken(
                        taken=False, reason="an earlier snapshot has not completed"
                    )
                existing = snapshots.begin(
                    disk_id=disk_id,
                    workspace_id=str(row.workspace_id),
                    generation=generation,
                    token=secrets.token_hex(16),
                    provider_ref=row.volume_provider_ref,
                    connection_id=(
                        str(row.volume_connection_id)
                        if row.volume_connection_id is not None
                        else None
                    ),
                    capacity_workspace_id=row.volume_capacity_workspace_id,
                    region=row.volume_region,
                    volume_size_bytes=row.volume_size_bytes,
                    due_at=now + timedelta(seconds=DISK_SNAPSHOT_CREATE_SILENCE_SECONDS),
                )
            request = BlockSnapshotRequest(
                owner=BlockVolumeOwner(
                    deployment=self.deployment,
                    workspace_id=str(row.workspace_id),
                    disk_id=disk_id,
                ),
                volume_id=row.volume_id,
                generation=generation,
                token=existing.token,
            )
        try:
            made = self._provider(existing).create_snapshot(request)
        except BlockVolumeMissingError as exc:
            raise ConflictError(f"disk {disk_id} volume {request.volume_id} is gone") from exc
        except BlockVolumePendingError as exc:
            if final:
                raise DiskVolumePendingError(
                    f"disk {disk_id} volume was snapshotted moments ago; retry shortly"
                ) from exc
            return DiskSnapshotTaken(taken=False, reason=str(exc))
        with self.database.session() as session:
            recorded = DiskSnapshotRepository(session).record_created(
                existing.id,
                snapshot_id=made.snapshot_id,
                due_at=now + timedelta(seconds=DISK_SNAPSHOT_POLL_SECONDS),
            )
        if not recorded:
            # The sweep gave up on this creation first; orphan collection deletes it.
            return DiskSnapshotTaken(taken=False, reason="the creation was settled without it")
        LOGGER.info(
            "disk %s snapshot %s started at generation %d", disk_id, made.snapshot_id, generation
        )
        return DiskSnapshotTaken(taken=True, snapshot_id=made.snapshot_id)

    def reconcile_due(self, *, now: datetime | None = None, limit: int = 100) -> None:
        current = to_utc(now or self.clock())
        with self.database.session() as session:
            due = DiskSnapshotRepository(session).due(now=current, limit=limit)
        for row in due:
            try:
                self._settle(row, now=current)
            except Exception:
                LOGGER.exception(
                    "disk snapshot housekeeping failed; retrying next pass",
                    extra={"disk_id": row.disk_id, "snapshot_id": row.snapshot_id},
                )
        self._collect_orphans_when_due(now=current)

    def remove_all(self, disk_id: str) -> None:
        """Delete every snapshot of a deleting disk; raises until the provider has them all."""
        with self.database.session() as session:
            rows = DiskSnapshotRepository(session).of_disk(disk_id)
        waiting = 0
        for row in rows:
            provider = self._provider(row)
            snapshot_id = row.snapshot_id
            if row.state == "creating":
                made = provider.find_snapshot(token=row.token)
                snapshot_id = made.snapshot_id if made is not None else ""
            try:
                if snapshot_id:
                    provider.delete_snapshot(snapshot_id)
            except BlockVolumePendingError:
                waiting += 1
                continue
            with self.database.session() as session:
                DiskSnapshotRepository(session).remove(row.id, state=row.state)
        if waiting:
            raise DiskVolumePendingError(
                f"disk {disk_id} has {waiting} snapshots the provider cannot delete yet; retry"
            )

    def collect_orphans(self) -> int:
        """Delete this deployment's snapshots that no row records; returns how many."""
        with self.database.session() as session:
            scopes = {
                (scope.provider_ref, scope.region): scope
                for scope in DiskSnapshotRepository(session).scopes()
            }
            for volume_scope in DiskVolumeRepository(session).scopes():
                scopes.setdefault(
                    (volume_scope.provider_ref, volume_scope.region),
                    DiskSnapshotScope(
                        workspace_id=volume_scope.workspace_id,
                        provider_ref=volume_scope.provider_ref,
                        region=volume_scope.region,
                    ),
                )
        removed = 0
        for scope in scopes.values():
            try:
                removed += self._collect_scope(scope)
            except Exception:
                LOGGER.exception(
                    "disk snapshot orphan collection failed for one account; retrying later",
                    extra={"provider_ref": scope.provider_ref, "region": scope.region},
                )
        return removed

    def _collect_scope(self, scope: DiskSnapshotScope) -> int:
        provider = self.providers.volumes(
            BlockVolumeScope(
                workspace_id=scope.workspace_id,
                provider_ref=scope.provider_ref,
                region=scope.region,
            )
        )
        listed = provider.describe_snapshots(deployment=self.deployment)
        cutoff = self.clock() - timedelta(seconds=DISK_VOLUME_ORPHAN_MIN_AGE_SECONDS)
        settled = tuple(
            snapshot
            for snapshot in listed
            if snapshot.created_at is not None and to_utc(snapshot.created_at) <= cutoff
        )
        with self.database.session() as session:
            ids, tokens = DiskSnapshotRepository(session).recorded(
                provider_ref=scope.provider_ref, region=scope.region
            )
        removed = 0
        for orphan in orphaned_snapshots(
            settled, deployment=self.deployment, recorded_ids=ids, recorded_tokens=tokens
        ):
            LOGGER.warning(
                "deleting a disk snapshot no disk records",
                extra={
                    "snapshot_id": orphan.snapshot_id,
                    "disk_id": orphan.owner.disk_id if orphan.owner else "",
                    "provider_ref": scope.provider_ref,
                    "region": scope.region,
                },
            )
            provider.delete_snapshot(orphan.snapshot_id)
            removed += 1
        return removed

    def _collect_orphans_when_due(self, *, now: datetime) -> None:
        if self._orphans_due_at is not None and now < self._orphans_due_at:
            return
        self._orphans_due_at = now + timedelta(seconds=DISK_SNAPSHOT_ORPHAN_INTERVAL_SECONDS)
        self.collect_orphans()

    def _settle(self, row: DiskSnapshotRow, *, now: datetime) -> None:
        provider = self._provider(row)
        match row.state:
            case "creating":
                made = provider.find_snapshot(token=row.token)
                with self.database.session() as session:
                    snapshots = DiskSnapshotRepository(session)
                    if made is None:
                        snapshots.remove(row.id, state="creating")
                    else:
                        snapshots.record_created(row.id, snapshot_id=made.snapshot_id, due_at=now)
            case "pending":
                described = provider.describe_snapshot(row.snapshot_id)
                with self.database.session() as session:
                    snapshots = DiskSnapshotRepository(session)
                    if described is None:
                        snapshots.remove(row.id, state="pending")
                    elif described.state is BlockSnapshotState.Pending:
                        snapshots.defer(
                            row.id,
                            state="pending",
                            due_at=now + timedelta(seconds=DISK_SNAPSHOT_POLL_SECONDS),
                        )
                    elif described.state is BlockSnapshotState.Error:
                        LOGGER.warning(
                            "disk snapshot failed at the provider; deleting it",
                            extra={"disk_id": row.disk_id, "snapshot_id": row.snapshot_id},
                        )
                        snapshots.mark_deleting(row.id, due_at=now)
                    elif snapshots.complete(row.id, stored_bytes=described.stored_bytes, at=now):
                        snapshots.supersede(row.disk_id, generation=row.generation, due_at=now)
            case "deleting":
                try:
                    provider.delete_snapshot(row.snapshot_id)
                except BlockVolumePendingError:
                    with self.database.session() as session:
                        DiskSnapshotRepository(session).defer(
                            row.id,
                            state="deleting",
                            due_at=now + timedelta(seconds=DISK_SNAPSHOT_DELETE_RETRY_SECONDS),
                        )
                    return
                with self.database.session() as session:
                    DiskSnapshotRepository(session).remove(row.id, state="deleting")

    def _provider(self, row: DiskSnapshotRow) -> BlockVolumeProvider:
        return self.providers.volumes(
            BlockVolumeScope(
                workspace_id=row.capacity_workspace_id,
                provider_ref=row.provider_ref,
                region=row.region,
            )
        )


__all__ = [
    "DISK_SNAPSHOT_CREATE_SILENCE_SECONDS",
    "DISK_SNAPSHOT_DELETE_RETRY_SECONDS",
    "DISK_SNAPSHOT_ORPHAN_INTERVAL_SECONDS",
    "DISK_SNAPSHOT_POLL_SECONDS",
    "DiskSnapshotService",
    "DiskSnapshotTaken",
]
