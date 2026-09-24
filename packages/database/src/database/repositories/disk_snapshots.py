from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.tables.disks import DiskGenerationTable, DiskSnapshotTable, DiskTable
from shared.timestamps import to_utc
from sqlalchemy import BigInteger, Select, and_, cast, delete, func, select, update
from sqlalchemy.orm import Session

BILLED_SNAPSHOT_STATES = ("pending", "completed")
"""States in which a snapshot's stored bytes are the disk's; one being deleted no longer is."""


@dataclass(frozen=True, slots=True)
class DiskSnapshotRow:
    id: str
    disk_id: str
    workspace_id: str
    generation: int
    state: str
    snapshot_id: str
    token: str
    provider_ref: str
    connection_id: str | None
    capacity_workspace_id: str
    region: str
    volume_size_bytes: int
    stored_bytes: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DiskSnapshotScope:
    workspace_id: str
    provider_ref: str
    region: str


def _select() -> Select[tuple[DiskSnapshotTable]]:
    # Rows move through fenced updates that bypass the session, so a read never
    # trusts a copy the session already holds.
    return select(DiskSnapshotTable).execution_options(populate_existing=True)


def _row(table: DiskSnapshotTable) -> DiskSnapshotRow:
    return DiskSnapshotRow(
        id=str(table.id),
        disk_id=str(table.disk_id),
        workspace_id=str(table.workspace_id),
        generation=table.generation,
        state=table.state,
        snapshot_id=table.snapshot_id,
        token=table.token,
        provider_ref=table.provider_ref,
        connection_id=str(table.connection_id) if table.connection_id is not None else None,
        capacity_workspace_id=table.capacity_workspace_id,
        region=table.region,
        volume_size_bytes=table.volume_size_bytes,
        stored_bytes=table.stored_bytes,
        created_at=to_utc(table.created_at),
    )


@dataclass(slots=True)
class DiskSnapshotRepository:
    session: Session

    def get(self, snapshot_row_id: str) -> DiskSnapshotRow | None:
        found = self.session.scalar(_select().where(DiskSnapshotTable.id == snapshot_row_id))
        return _row(found) if found is not None else None

    def for_generation(
        self, disk_id: str, *, generation: int, provider_ref: str, region: str
    ) -> DiskSnapshotRow | None:
        found = self.session.scalar(
            _select().where(
                DiskSnapshotTable.disk_id == disk_id,
                DiskSnapshotTable.generation == generation,
                DiskSnapshotTable.provider_ref == provider_ref,
                DiskSnapshotTable.region == region,
            )
        )
        return _row(found) if found is not None else None

    def in_flight(self, disk_id: str) -> bool:
        """Whether any snapshot of the disk is still being created or has not completed."""
        return bool(
            self.session.scalar(
                select(
                    select(DiskSnapshotTable.id)
                    .where(
                        DiskSnapshotTable.disk_id == disk_id,
                        DiskSnapshotTable.state.in_(("creating", "pending")),
                    )
                    .exists()
                )
            )
        )

    def begin(
        self,
        *,
        disk_id: str,
        workspace_id: str,
        generation: int,
        token: str,
        provider_ref: str,
        connection_id: str | None,
        capacity_workspace_id: str,
        region: str,
        volume_size_bytes: int,
        due_at: datetime,
    ) -> DiskSnapshotRow:
        table = DiskSnapshotTable(
            disk_id=disk_id,
            workspace_id=workspace_id,
            generation=generation,
            state="creating",
            snapshot_id="",
            token=token,
            provider_ref=provider_ref,
            connection_id=connection_id,
            capacity_workspace_id=capacity_workspace_id,
            region=region,
            volume_size_bytes=volume_size_bytes,
            stored_bytes=0,
            due_at=due_at,
        )
        self.session.add(table)
        self.session.flush()
        return _row(table)

    def record_created(self, snapshot_row_id: str, *, snapshot_id: str, due_at: datetime) -> bool:
        """Name the provider snapshot a creation made; only a row still creating moves."""
        return self._move(
            snapshot_row_id,
            from_states=("creating",),
            values={"state": "pending", "snapshot_id": snapshot_id, "due_at": due_at},
        )

    def complete(self, snapshot_row_id: str, *, stored_bytes: int, at: datetime) -> bool:
        return self._move(
            snapshot_row_id,
            from_states=("pending",),
            values={
                "state": "completed",
                "stored_bytes": stored_bytes,
                "completed_at": at,
                "due_at": None,
            },
        )

    def defer(self, snapshot_row_id: str, *, state: str, due_at: datetime) -> None:
        self._move(snapshot_row_id, from_states=(state,), values={"due_at": due_at})

    def mark_deleting(self, snapshot_row_id: str, *, due_at: datetime) -> bool:
        return self._move(
            snapshot_row_id,
            from_states=("pending", "completed", "deleting"),
            values={"state": "deleting", "due_at": due_at},
        )

    def supersede(self, disk_id: str, *, generation: int, due_at: datetime) -> int:
        """Mark every snapshot of the disk older than `generation` for deletion.

        One a volume creation is reading from stays until that creation settles.
        A snapshot still being created is left to finish, and goes once it has.
        """
        reading = select(DiskTable.volume_source_snapshot_id).where(
            DiskTable.id == disk_id, DiskTable.volume_state == "creating"
        )
        moved = self.session.scalars(
            update(DiskSnapshotTable)
            .where(
                DiskSnapshotTable.disk_id == disk_id,
                DiskSnapshotTable.generation < generation,
                DiskSnapshotTable.state.in_(("pending", "completed")),
                DiskSnapshotTable.snapshot_id.not_in(reading.scalar_subquery()),
            )
            .values(state="deleting", due_at=due_at)
            .returning(DiskSnapshotTable.id)
            .execution_options(synchronize_session=False)
        ).all()
        return len(moved)

    def remove(self, snapshot_row_id: str, *, state: str) -> bool:
        removed = self.session.scalar(
            delete(DiskSnapshotTable)
            .where(DiskSnapshotTable.id == snapshot_row_id, DiskSnapshotTable.state == state)
            .returning(DiskSnapshotTable.id)
        )
        return removed is not None

    def due(self, *, now: datetime, limit: int) -> tuple[DiskSnapshotRow, ...]:
        """Snapshots whose next look is due, in one indexed read however many have completed."""
        rows = self.session.scalars(
            _select()
            .where(
                DiskSnapshotTable.state.in_(("creating", "pending", "deleting")),
                DiskSnapshotTable.due_at <= now,
            )
            .order_by(DiskSnapshotTable.due_at, DiskSnapshotTable.id)
            .limit(limit)
        )
        return tuple(_row(found) for found in rows)

    def of_disk(self, disk_id: str) -> tuple[DiskSnapshotRow, ...]:
        rows = self.session.scalars(
            _select()
            .where(DiskSnapshotTable.disk_id == disk_id)
            .order_by(DiskSnapshotTable.generation, DiskSnapshotTable.id)
        )
        return tuple(_row(found) for found in rows)

    def restore_source(
        self,
        disk_id: str,
        *,
        provider_ref: str,
        region: str,
        connection_id: str | None,
        max_volume_bytes: int,
    ) -> str:
        """The snapshot a new volume for the disk in this account and region starts from.

        The newest completed one no larger than the volume, whose generation is
        still in the published chain: no newer than the disk's generation and no
        older than its newest self-contained one. Empty when there is none.
        """
        chain_base = (
            select(func.coalesce(func.max(DiskGenerationTable.generation), 0))
            .where(
                DiskGenerationTable.disk_id == disk_id,
                DiskGenerationTable.parent_generation == 0,
            )
            .scalar_subquery()
        )
        found = self.session.scalar(
            select(DiskSnapshotTable.snapshot_id)
            .join(DiskTable, DiskTable.id == DiskSnapshotTable.disk_id)
            .where(
                DiskSnapshotTable.disk_id == disk_id,
                DiskSnapshotTable.state == "completed",
                DiskSnapshotTable.provider_ref == provider_ref,
                DiskSnapshotTable.region == region,
                DiskSnapshotTable.connection_id.is_not_distinct_from(connection_id),
                DiskSnapshotTable.volume_size_bytes <= max_volume_bytes,
                DiskSnapshotTable.generation <= DiskTable.generation,
                DiskSnapshotTable.generation >= chain_base,
            )
            .order_by(DiskSnapshotTable.generation.desc())
            .limit(1)
        )
        return found or ""

    def forget_source(self, disk_id: str, snapshot_id: str, *, due_at: datetime) -> None:
        """Stop offering a snapshot a volume creation found missing; the sweep removes its row."""
        self.session.execute(
            update(DiskSnapshotTable)
            .where(
                DiskSnapshotTable.disk_id == disk_id,
                DiskSnapshotTable.snapshot_id == snapshot_id,
            )
            .values(state="deleting", due_at=due_at)
            .execution_options(synchronize_session=False)
        )

    def scopes(self) -> tuple[DiskSnapshotScope, ...]:
        rows = self.session.execute(
            select(
                DiskSnapshotTable.provider_ref,
                DiskSnapshotTable.region,
                func.min(DiskSnapshotTable.capacity_workspace_id),
            ).group_by(DiskSnapshotTable.provider_ref, DiskSnapshotTable.region)
        ).tuples()
        return tuple(
            DiskSnapshotScope(workspace_id=workspace_id, provider_ref=provider_ref, region=region)
            for provider_ref, region, workspace_id in rows
        )

    def recorded(self, *, provider_ref: str, region: str) -> tuple[frozenset[str], frozenset[str]]:
        """Every snapshot id and creation token recorded in one account and region."""
        rows = self.session.execute(
            select(DiskSnapshotTable.snapshot_id, DiskSnapshotTable.token).where(
                DiskSnapshotTable.provider_ref == provider_ref,
                DiskSnapshotTable.region == region,
            )
        ).tuples()
        ids: set[str] = set()
        tokens: set[str] = set()
        for snapshot_id, token in rows:
            if snapshot_id:
                ids.add(snapshot_id)
            tokens.add(token)
        return frozenset(ids), frozenset(tokens)

    def _move(
        self, snapshot_row_id: str, *, from_states: tuple[str, ...], values: dict[str, object]
    ) -> bool:
        moved = self.session.scalar(
            update(DiskSnapshotTable)
            .where(
                and_(
                    DiskSnapshotTable.id == snapshot_row_id,
                    DiskSnapshotTable.state.in_(from_states),
                )
            )
            .values(values)
            .returning(DiskSnapshotTable.id)
            .execution_options(synchronize_session=False)
        )
        return moved is not None


def billed_snapshot_bytes() -> Select[tuple[int]]:
    """A correlated sum of a disk row's billed snapshot bytes, for selecting beside it.

    PostgreSQL sums bigints as numeric, so the total is cast back.
    """
    return select(
        cast(func.coalesce(func.sum(DiskSnapshotTable.stored_bytes), 0), BigInteger)
    ).where(
        DiskSnapshotTable.disk_id == DiskTable.id,
        DiskSnapshotTable.state.in_(BILLED_SNAPSHOT_STATES),
    )


__all__ = [
    "BILLED_SNAPSHOT_STATES",
    "DiskSnapshotRepository",
    "DiskSnapshotRow",
    "DiskSnapshotScope",
    "billed_snapshot_bytes",
]
