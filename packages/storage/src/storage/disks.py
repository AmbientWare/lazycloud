"""Durable disks: which container may write a disk, and the generations it published.

One writer at a time is the whole safety argument, because two writers of one
block device corrupt it rather than conflict. A lease names the container and
carries a token minted at acquisition; a publish or release that carries any
other token is refused. A holder keeps the lease until its final publish or
release, or until the durable record shows it can no longer make one: the
container is stopped and its worker released its storage, or that worker is
gone for good. Releasing any earlier would let the next container restore a
generation the stopping one was still about to supersede.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from billing.admission import DatabaseBillingAdmission
from database.repositories.disks import DiskChainLink, DiskHolder, DiskRepository
from database.repositories.identity import WorkspaceRepository
from database.tables.disks import DiskTable
from database.types import DatabaseSession
from shared.disks import (
    DiskMount,
    DiskRecord,
    DiskStatus,
    disk_manifest_key,
)
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.timestamps import to_utc, utc_now

from database import DatabaseClient
from storage.disk_volumes import DiskVolumeService, DiskWorkerAbsence, holder_keeps_disk

LOGGER = logging.getLogger(__name__)

DISK_LIST_LIMIT = 100


class DiskObjectStore(Protocol):
    def delete_disk_objects(self, *, workspace_id: str, disk_id: str) -> None: ...


class DiskDeletionMetering(Protocol):
    def finalize_disk_deletion(self, disk_id: str, *, workspace_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DiskAcquisition:
    disk_id: str
    lease_token: str
    size_bytes: int
    generation: int
    chain: tuple[DiskChainLink, ...]


@dataclass(frozen=True, slots=True)
class DiskPublication:
    disk_id: str
    container_id: str
    lease_token: str
    generation: int
    parent_generation: int
    manifest_key: str
    manifest_sha256: str
    stored_bytes_added: int


@dataclass(frozen=True, slots=True)
class DiskPage:
    data: tuple[DiskRecord, ...]
    next: str


@dataclass(frozen=True, slots=True)
class ResolvedDisk:
    record: DiskRecord
    mount: DiskMount
    last_worker_id: str
    volume_zone: str


def get_or_create_disks(
    database: DatabaseClient, mounts: list[DiskMount], *, workspace_id: str
) -> list[ResolvedDisk]:
    """The workload's disks by name, each created on its first use.

    A larger declared size grows the disk: it is recorded here, and the next
    container to acquire the disk gets a volume of the new size and grows the
    filesystem into it. A smaller one is not refused here. Stub creation refuses
    it before a deploy, and a container launched from an older stub, whether a
    rollback or a previous version restarting mid-rollout, gets the disk at the
    size it has, which the returned record carries.
    """
    if not mounts:
        return []
    resolved: list[ResolvedDisk] = []
    with database.session() as session:
        repository = DiskRepository(session)
        created = False
        for mount in mounts:
            record, inserted = repository.get_or_create(
                mount.name, workspace_id=workspace_id, size_bytes=mount.size_bytes
            )
            created = created or inserted
            if mount.size_bytes > record.size_bytes:
                row = repository.lock(record.id)
                if row is None:
                    raise ConflictError("disk changed during resolution; retry the request")
                row.size_bytes = mount.size_bytes
                row.updated_at = utc_now()
                record = record.model_copy(update={"size_bytes": mount.size_bytes})
            resolved.append(
                ResolvedDisk(record=record, mount=mount, last_worker_id="", volume_zone="")
            )
        if created:
            # Asked in the transaction that inserted, so a refusal leaves no disk behind.
            DatabaseBillingAdmission().assert_may_take_on_billed_work(
                session, workspace_id=workspace_id
            )
        DatabaseBillingAdmission().assert_disk_allowance(
            session,
            workspace_id=workspace_id,
            declared_bytes=repository.declared_bytes(workspace_id),
        )
        hints = repository.placement_hints(
            [mount.name for mount in mounts], workspace_id=workspace_id
        )
    return [
        ResolvedDisk(
            record=item.record,
            mount=item.mount,
            last_worker_id=hint.last_worker_id if (hint := hints.get(item.mount.name)) else "",
            volume_zone=hint.volume_zone if hint else "",
        )
        for item in resolved
    ]


@dataclass(slots=True)
class DiskService:
    database: DatabaseClient
    worker_absence: DiskWorkerAbsence

    def get(self, name: str, *, workspace_id: str) -> DiskRecord:
        with self.database.session() as session:
            record = DiskRepository(session).get(name, workspace_id=workspace_id)
        if record is None:
            raise NotFoundError(f"disk not found: {name}")
        return record

    def list(self, *, workspace_id: str, after: str = "", limit: int = DISK_LIST_LIMIT) -> DiskPage:
        bounded = max(1, min(limit, DISK_LIST_LIMIT))
        with self.database.session() as session:
            records = DiskRepository(session).list(
                workspace_id=workspace_id, after=after, limit=bounded
            )
        return DiskPage(
            data=tuple(records),
            next=records[-1].name if len(records) == bounded else "",
        )

    def identity(self, disk_id: str) -> tuple[str, str] | None:
        with self.database.session() as session:
            return DiskRepository(session).identity(disk_id)

    def acquire(self, disk_id: str, *, container_id: str, worker_id: str) -> DiskAcquisition:
        with self.database.session() as session:
            repository = DiskRepository(session)
            row = self._lock_live(repository, disk_id)
            holder_id = str(row.holder_container_id or "")
            if holder_id == container_id and row.lease_token:
                # A retried acquire keeps its lease, so the volume work the first
                # attempt started continues under it instead of being fenced out.
                return DiskAcquisition(
                    disk_id=disk_id,
                    lease_token=row.lease_token,
                    size_bytes=row.size_bytes,
                    generation=row.generation,
                    chain=tuple(repository.chain(disk_id)),
                )
            if holder_id and holder_id != container_id:
                holder = repository.holder(holder_id)
                if self._holds(holder):
                    raise ConflictError(
                        f"disk {row.name} is held by container {holder_id} until it publishes "
                        "its final generation"
                    )
            token = secrets.token_hex(32)
            row.holder_container_id = container_id
            row.lease_token = token
            row.status = DiskStatus.Attached.value
            row.last_worker_id = worker_id
            row.updated_at = utc_now()
            session.flush()
            repository.open_attachment(
                disk_id,
                workspace_id=str(row.workspace_id),
                container_id=container_id,
                size_bytes=row.size_bytes,
                at=row.updated_at,
            )
            return DiskAcquisition(
                disk_id=disk_id,
                lease_token=token,
                size_bytes=row.size_bytes,
                generation=row.generation,
                chain=tuple(repository.chain(disk_id)),
            )

    def publish(self, publication: DiskPublication) -> int:
        with self.database.session() as session:
            repository = DiskRepository(session)
            row = self._lock_live(repository, publication.disk_id)
            if publication.generation <= row.generation:
                # A retry of a publish whose answer was lost. The manifest digest
                # names the layer, so a match is that same publish and changes nothing.
                recorded = repository.published_manifest_sha256(
                    publication.disk_id, publication.generation
                )
                if recorded != publication.manifest_sha256:
                    raise ConflictError(
                        f"disk {row.name} is at generation {row.generation}; generation "
                        f"{publication.generation} cannot be published again"
                    )
                return publication.generation
            self._require_lease(row, publication.container_id, publication.lease_token)
            self._record_generation(repository, row, publication)
            return publication.generation

    def release(self, disk_id: str, *, container_id: str, lease_token: str) -> bool:
        """Give the lease back; a token that is no longer current releases nothing."""
        with self.database.session() as session:
            repository = DiskRepository(session)
            row = repository.lock(disk_id)
            if row is None or row.deleted_at is not None:
                return False
            if not self._lease_matches(row, container_id, lease_token):
                return False
            self._release(repository, row)
            return True

    def collect(
        self,
        disk_id: str,
        *,
        container_id: str,
        lease_token: str,
        generation: int,
        stored_bytes_removed: int,
    ) -> None:
        """Record what the holder deleted once a self-contained generation superseded it.

        Only the current holder, and only for the disk's newest generation when
        that generation stands alone: anything older may still be restored from,
        and a stale holder may be collecting under a disk someone else writes.
        """
        with self.database.session() as session:
            repository = DiskRepository(session)
            row = self._lock_live(repository, disk_id)
            self._require_lease(row, container_id, lease_token)
            if generation != row.generation:
                raise ConflictError(
                    f"disk {row.name} is at generation {row.generation}; only it can be "
                    f"collected under, not {generation}"
                )
            if repository.parent_generation(disk_id, generation) != 0:
                raise ConflictError(
                    f"disk {row.name} generation {generation} builds on another layer; "
                    "only a self-contained generation supersedes older ones"
                )
            row.stored_bytes = max(0, row.stored_bytes - stored_bytes_removed)
            row.updated_at = utc_now()
            repository.delete_generations_below(disk_id, generation)

    def request_deletion(self, name: str, *, workspace_id: str, now: datetime | None = None) -> str:
        """Record the intent to delete; the name is free and metering stops from here."""
        with self.database.session() as session:
            return self.request_deletion_in_session(
                session, name, workspace_id=workspace_id, now=now
            )

    def request_deletion_in_session(
        self,
        session: DatabaseSession,
        name: str,
        *,
        workspace_id: str,
        now: datetime | None = None,
    ) -> str:
        WorkspaceRepository(session).lock_storage_accounting_owner(workspace_id)
        repository = DiskRepository(session)
        row = repository.lock_by_name(name, workspace_id=workspace_id)
        if row is None:
            raise NotFoundError(f"disk not found: {name}")
        holder_id = str(row.holder_container_id or "")
        if holder_id and self._holds(repository.holder(holder_id)):
            raise ConflictError(f"stop container {holder_id} before deleting disk {name}")
        row.holder_container_id = None
        row.lease_token = ""
        row.status = DiskStatus.Deleting.value
        deleted_at = to_utc(now or utc_now())
        row.deleted_at = deleted_at
        row.updated_at = utc_now()
        repository.close_attachment(str(row.id), at=deleted_at)
        return str(row.id)

    @staticmethod
    def _lock_live(repository: DiskRepository, disk_id: str) -> DiskTable:
        row = repository.lock(disk_id)
        if row is None:
            raise NotFoundError(f"disk not found: {disk_id}")
        if row.deleted_at is not None:
            raise ConflictError(f"disk {row.name} is deleting")
        return row

    @staticmethod
    def _lease_matches(row: DiskTable, container_id: str, lease_token: str) -> bool:
        return str(row.holder_container_id or "") == container_id and secrets.compare_digest(
            row.lease_token, lease_token
        )

    @classmethod
    def _require_lease(cls, row: DiskTable, container_id: str, lease_token: str) -> None:
        if not cls._lease_matches(row, container_id, lease_token):
            raise ConflictError(
                f"container {container_id} no longer holds disk {row.name}; its lease was "
                "released or taken over"
            )

    @staticmethod
    def _record_generation(
        repository: DiskRepository, row: DiskTable, publication: DiskPublication
    ) -> None:
        if publication.generation != row.generation + 1:
            raise ConflictError(
                f"disk {row.name} is at generation {row.generation}; the next publish must be "
                f"{row.generation + 1}, not {publication.generation}"
            )
        if publication.parent_generation not in {0, row.generation}:
            raise ConflictError(
                f"a layer for disk {row.name} must build on generation {row.generation} or on "
                f"nothing, not {publication.parent_generation}"
            )
        expected_key = disk_manifest_key(str(row.id), publication.generation)
        if publication.manifest_key != expected_key:
            raise InvalidInputError(f"disk manifest key must be {expected_key}")
        repository.add_generation(
            str(row.id),
            generation=publication.generation,
            parent_generation=publication.parent_generation,
            manifest_key=publication.manifest_key,
            manifest_sha256=publication.manifest_sha256,
            stored_bytes_added=publication.stored_bytes_added,
        )
        row.generation = publication.generation
        row.stored_bytes = row.stored_bytes + publication.stored_bytes_added
        row.updated_at = utc_now()

    @staticmethod
    def _release(repository: DiskRepository, row: DiskTable) -> None:
        row.holder_container_id = None
        row.lease_token = ""
        row.status = DiskStatus.Detached.value
        row.updated_at = utc_now()
        repository.close_attachment(str(row.id), at=row.updated_at)

    def _holds(self, holder: DiskHolder) -> bool:
        return holder_keeps_disk(holder, self.worker_absence)


@dataclass(slots=True)
class DiskDeletionService:
    """Removes a deleting disk's objects and then its rows, retrying until both are gone."""

    database: DatabaseClient
    disks: DiskService
    volumes: DiskVolumeService
    objects: DiskObjectStore
    metering: DiskDeletionMetering

    def request(self, name: str, *, workspace_id: str) -> bool:
        """Delete the disk now, or leave it deleting for the sweep; False when deferred."""
        disk_id = self.disks.request_deletion(name, workspace_id=workspace_id)
        try:
            self.finish(disk_id, workspace_id=workspace_id)
        except Exception:
            LOGGER.exception(
                "disk deletion queued for retry",
                extra={"workspace_id": workspace_id, "disk_id": disk_id},
            )
            return False
        return True

    def delete_workspace_disks(self, workspace_id: str) -> None:
        """Delete every disk of a workspace being deleted, volumes and objects included.

        Raises while any is left, so the workspace deletion retries rather than
        finishing over a disk whose volume still exists.
        """
        with self.database.session() as session:
            disks = DiskRepository(session).workspace_disks(workspace_id)
        for disk_id, name, deleting in disks:
            if not deleting:
                self.disks.request_deletion(name, workspace_id=workspace_id)
            self.finish(disk_id, workspace_id=workspace_id)

    def finish(self, disk_id: str, *, workspace_id: str) -> None:
        self.metering.finalize_disk_deletion(disk_id, workspace_id=workspace_id)
        self.volumes.remove(disk_id)
        self.objects.delete_disk_objects(workspace_id=workspace_id, disk_id=disk_id)
        with self.database.session() as session:
            DiskRepository(session).delete(disk_id)

    def reconcile_due(self, *, now: datetime | None = None, limit: int = 100) -> None:
        del now
        with self.database.session() as session:
            targets = DiskRepository(session).list_deletions(limit=limit)
        for workspace_id, disk_id in targets:
            try:
                self.finish(str(disk_id), workspace_id=str(workspace_id))
            except Exception:
                LOGGER.exception(
                    "disk deletion failed; it will be retried",
                    extra={"workspace_id": str(workspace_id), "disk_id": str(disk_id)},
                )


__all__ = [
    "DISK_LIST_LIMIT",
    "DiskAcquisition",
    "DiskDeletionMetering",
    "DiskDeletionService",
    "DiskObjectStore",
    "DiskPage",
    "DiskPublication",
    "DiskService",
    "ResolvedDisk",
    "get_or_create_disks",
]
