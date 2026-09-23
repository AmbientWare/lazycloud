from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.identity import WorkspaceRepository
from database.tables.disks import DiskGenerationTable, DiskTable
from database.tables.identity import WorkspaceTable
from database.tables.orchestration import ContainerTable
from shared.containers import LIVE_CONTAINER_STATUSES, ContainerStatus
from shared.disks import DiskRecord, DiskStatus
from shared.errors import ConflictError
from shared.timestamps import to_utc
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class DiskHolder:
    """What the durable record says about the container holding a disk."""

    container_id: str
    status: ContainerStatus | None
    """None when the container row no longer exists."""

    storage_released: bool
    worker_id: str


@dataclass(frozen=True, slots=True)
class DiskChainLink:
    generation: int
    manifest_key: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class DiskMeteringTarget:
    id: str
    workspace_id: str
    name: str


@dataclass(frozen=True, slots=True)
class DiskMeteringCheckpoint:
    id: str
    workspace_id: str
    name: str
    stored_bytes: int
    metered_bytes: int
    metered_at: datetime
    deleted_at: datetime | None


def disk_from_table(row: DiskTable, *, holder_live: bool = True) -> DiskRecord:
    """The disk as a reader sees it; a holder that stopped no longer holds it.

    The row keeps a terminal holder until the next acquire clears it, since
    only acquisition decides when its final publish is past. A reader is told
    the disk is detached from the moment the holder stops.
    """
    status = DiskStatus(row.status)
    held = status is DiskStatus.Attached and holder_live
    return DiskRecord(
        id=str(row.id),
        name=row.name,
        size_bytes=row.size_bytes,
        status=DiskStatus.Detached if status is DiskStatus.Attached and not held else status,
        generation=row.generation,
        stored_bytes=row.stored_bytes,
        holder_container_id=str(row.holder_container_id or "") if held else "",
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


@dataclass(slots=True)
class DiskRepository:
    session: Session

    def get_or_create(
        self, name: str, *, workspace_id: str, size_bytes: int
    ) -> tuple[DiskRecord, bool]:
        """Return the named live disk and whether this transaction created it."""
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        created = self.session.scalar(
            postgresql_insert(DiskTable)
            .values(
                workspace_id=workspace_id,
                name=name,
                size_bytes=size_bytes,
                status=DiskStatus.Detached.value,
                generation=0,
                stored_bytes=0,
                lease_token="",
                last_worker_id="",
                metered_bytes=0,
            )
            .on_conflict_do_nothing(
                index_elements=[DiskTable.workspace_id, DiskTable.name],
                index_where=DiskTable.deleted_at.is_(None),
            )
            .returning(DiskTable)
        )
        if created is not None:
            return disk_from_table(created), True
        existing = self.get(name, workspace_id=workspace_id)
        if existing is None:
            raise ConflictError("disk changed during creation; retry the request")
        return existing, False

    def get(self, name: str, *, workspace_id: str) -> DiskRecord | None:
        result = self.session.execute(
            _with_holder_liveness().where(
                DiskTable.workspace_id == workspace_id,
                DiskTable.name == name,
                DiskTable.deleted_at.is_(None),
            )
        ).first()
        if result is None:
            return None
        row, holder_live = result
        return disk_from_table(row, holder_live=bool(holder_live))

    def identity(self, disk_id: str) -> tuple[str, str] | None:
        """The live disk's workspace and name, resolved across workspaces."""
        row = self.session.execute(
            select(DiskTable.workspace_id, DiskTable.name).where(
                DiskTable.id == disk_id,
                DiskTable.deleted_at.is_(None),
            )
        ).first()
        if row is None:
            return None
        workspace_id, name = row
        return str(workspace_id), name

    def last_worker_ids(self, names: list[str], *, workspace_id: str) -> dict[str, str]:
        if not names:
            return {}
        rows = self.session.execute(
            select(DiskTable.name, DiskTable.last_worker_id).where(
                DiskTable.workspace_id == workspace_id,
                DiskTable.name.in_(names),
                DiskTable.deleted_at.is_(None),
            )
        ).tuples()
        return dict(rows.all())

    def list(self, *, workspace_id: str, after: str, limit: int) -> list[DiskRecord]:
        statement = _with_holder_liveness().where(
            DiskTable.workspace_id == workspace_id,
            DiskTable.deleted_at.is_(None),
        )
        if after:
            statement = statement.where(DiskTable.name > after)
        rows = self.session.execute(statement.order_by(DiskTable.name).limit(limit)).tuples()
        return [disk_from_table(row, holder_live=bool(live)) for row, live in rows]

    def lock(self, disk_id: str) -> DiskTable | None:
        return self.session.scalar(
            select(DiskTable)
            .where(DiskTable.id == disk_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def lock_by_name(self, name: str, *, workspace_id: str) -> DiskTable | None:
        return self.session.scalar(
            select(DiskTable)
            .where(
                DiskTable.workspace_id == workspace_id,
                DiskTable.name == name,
                DiskTable.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def holder(self, container_id: str) -> DiskHolder:
        row = self.session.execute(
            select(
                ContainerTable.status,
                ContainerTable.storage_released_at,
                ContainerTable.runtime_worker_id,
                ContainerTable.worker_id,
            ).where(ContainerTable.id == container_id)
        ).first()
        if row is None:
            return DiskHolder(
                container_id=container_id, status=None, storage_released=True, worker_id=""
            )
        status, storage_released_at, runtime_worker_id, worker_id = row
        return DiskHolder(
            container_id=container_id,
            status=ContainerStatus(status),
            storage_released=storage_released_at is not None,
            worker_id=runtime_worker_id or str(worker_id or ""),
        )

    def chain(self, disk_id: str) -> list[DiskChainLink]:
        """Published generations from the newest self-contained one to the newest."""
        base = (
            select(func.coalesce(func.max(DiskGenerationTable.generation), 0))
            .where(
                DiskGenerationTable.disk_id == disk_id,
                DiskGenerationTable.parent_generation == 0,
            )
            .scalar_subquery()
        )
        rows = self.session.execute(
            select(
                DiskGenerationTable.generation,
                DiskGenerationTable.manifest_key,
                DiskGenerationTable.manifest_sha256,
            )
            .where(
                DiskGenerationTable.disk_id == disk_id,
                DiskGenerationTable.generation >= base,
            )
            .order_by(DiskGenerationTable.generation)
        ).tuples()
        return [
            DiskChainLink(generation=generation, manifest_key=key, manifest_sha256=sha256)
            for generation, key, sha256 in rows
        ]

    def add_generation(
        self,
        disk_id: str,
        *,
        generation: int,
        parent_generation: int,
        manifest_key: str,
        manifest_sha256: str,
        stored_bytes_added: int,
    ) -> None:
        self.session.add(
            DiskGenerationTable(
                disk_id=disk_id,
                generation=generation,
                parent_generation=parent_generation,
                manifest_key=manifest_key,
                manifest_sha256=manifest_sha256,
                stored_bytes_added=stored_bytes_added,
            )
        )
        self.session.flush()

    def parent_generation(self, disk_id: str, generation: int) -> int | None:
        return self.session.scalar(
            select(DiskGenerationTable.parent_generation).where(
                DiskGenerationTable.disk_id == disk_id,
                DiskGenerationTable.generation == generation,
            )
        )

    def delete_generations_below(self, disk_id: str, generation: int) -> None:
        self.session.execute(
            delete(DiskGenerationTable).where(
                DiskGenerationTable.disk_id == disk_id,
                DiskGenerationTable.generation < generation,
            )
        )

    def published_manifest_sha256(self, disk_id: str, generation: int) -> str | None:
        return self.session.scalar(
            select(DiskGenerationTable.manifest_sha256).where(
                DiskGenerationTable.disk_id == disk_id,
                DiskGenerationTable.generation == generation,
            )
        )

    def list_deletions(self, *, limit: int) -> tuple[tuple[str, str], ...]:
        return tuple(
            self.session.execute(
                select(DiskTable.workspace_id, DiskTable.id)
                .where(DiskTable.deleted_at.is_not(None))
                .order_by(DiskTable.deleted_at, DiskTable.id)
                .limit(limit)
            ).tuples()
        )

    def delete(self, disk_id: str) -> None:
        self.session.execute(delete(DiskTable).where(DiskTable.id == disk_id))

    def list_metering_targets(
        self, *, metered_before: datetime, limit: int
    ) -> tuple[DiskMeteringTarget, ...]:
        rows = self.session.execute(
            select(DiskTable.id, DiskTable.workspace_id, DiskTable.name)
            .join(WorkspaceTable, WorkspaceTable.id == DiskTable.workspace_id)
            .where(DiskTable.deleted_at.is_(None), DiskTable.metered_at <= metered_before)
            .order_by(DiskTable.metered_at, DiskTable.id)
            .limit(limit)
        ).tuples()
        return tuple(
            DiskMeteringTarget(id=str(disk_id), workspace_id=str(workspace_id), name=name)
            for disk_id, workspace_id, name in rows
        )

    def lock_metering_checkpoint(self, disk_id: str) -> DiskMeteringCheckpoint | None:
        row = self.session.execute(
            select(
                DiskTable.workspace_id,
                DiskTable.name,
                DiskTable.stored_bytes,
                DiskTable.metered_bytes,
                DiskTable.metered_at,
                DiskTable.deleted_at,
            )
            .where(DiskTable.id == disk_id)
            .with_for_update()
        ).first()
        if row is None:
            return None
        workspace_id, name, stored_bytes, metered_bytes, metered_at, deleted_at = row
        return DiskMeteringCheckpoint(
            id=disk_id,
            workspace_id=str(workspace_id),
            name=name,
            stored_bytes=stored_bytes,
            metered_bytes=metered_bytes,
            metered_at=to_utc(metered_at),
            deleted_at=to_utc(deleted_at) if deleted_at is not None else None,
        )

    def advance_metering_checkpoint(
        self, disk_id: str, *, metered_bytes: int, metered_at: datetime
    ) -> None:
        self.session.execute(
            update(DiskTable)
            .where(DiskTable.id == disk_id)
            .values(metered_bytes=metered_bytes, metered_at=metered_at)
        )


def _with_holder_liveness() -> Select[tuple[DiskTable, bool]]:
    live = [status.value for status in LIVE_CONTAINER_STATUSES]
    return select(DiskTable, ContainerTable.status.in_(live)).outerjoin(
        ContainerTable, ContainerTable.id == DiskTable.holder_container_id
    )


__all__ = [
    "DiskChainLink",
    "DiskHolder",
    "DiskMeteringCheckpoint",
    "DiskMeteringTarget",
    "DiskRepository",
    "disk_from_table",
]
