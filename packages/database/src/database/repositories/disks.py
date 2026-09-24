from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.disk_snapshots import billed_snapshot_bytes
from database.repositories.identity import WorkspaceRepository
from database.tables.apps import AppTable, DeploymentTable, StubTable
from database.tables.compute import ComputeProviderInstanceTable
from database.tables.disks import DiskAttachmentTable, DiskGenerationTable, DiskTable
from database.tables.identity import WorkspaceTable
from database.tables.orchestration import ContainerTable, WorkerTable
from shared.containers import LIVE_CONTAINER_STATUSES, ContainerStatus
from shared.deployments import DeploymentKind, PodRole, StubKind
from shared.disks import DiskRecord, DiskStatus, DiskWorkload
from shared.errors import ConflictError
from shared.timestamps import to_utc
from sqlalchemy import Select, delete, func, or_, select, update
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
class DiskDeletionTarget:
    id: str
    workspace_id: str


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


@dataclass(frozen=True, slots=True)
class DiskPlacementHint:
    last_worker_id: str
    """The worker that last held the disk, which may still keep its layers."""

    volume_zone: str
    """The zone of the disk's volume while one exists; empty otherwise."""


@dataclass(frozen=True, slots=True)
class DiskAttachmentCheckpoint:
    """One lease's billing position, read under its row lock."""

    id: str
    disk_id: str
    workspace_id: str
    container_id: str
    size_bytes: int
    metered_at: datetime
    released_at: datetime | None
    holder_finished_at: datetime | None
    """When the holding container stopped running; it holds nothing billable after."""


@dataclass(frozen=True, slots=True)
class DiskReading:
    """A disk row with the container its lease names, read in the same query.

    The row keeps a stopped holder until the next acquire clears it, so the
    row's status alone cannot say whether the holder still saves. The storage
    owner decides that from `holder`.
    """

    row: DiskTable
    holder: DiskHolder | None
    snapshot_bytes: int
    """What the disk's live volume snapshots store beside its chunks."""


def disk_from_table(
    row: DiskTable, *, snapshot_bytes: int, status: DiskStatus | None = None
) -> DiskRecord:
    """The disk as a reader sees it, with the status its holder earns."""
    shown = status or DiskStatus(row.status)
    holding = shown in {DiskStatus.Attached, DiskStatus.Saving}
    return DiskRecord(
        id=str(row.id),
        name=row.name,
        size_bytes=row.size_bytes,
        status=shown,
        generation=row.generation,
        stored_bytes=row.stored_bytes + snapshot_bytes,
        holder_container_id=str(row.holder_container_id or "") if holding else "",
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


@dataclass(slots=True)
class DiskRepository:
    session: Session

    def get_or_create(
        self, name: str, *, workspace_id: str, size_bytes: int, stub_id: str
    ) -> tuple[DiskRecord, bool]:
        """Return the named live disk and whether this transaction created it.

        Either way the disk records `stub_id` as the workload asking for it.
        """
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
                last_stub_id=stub_id,
                metered_bytes=0,
            )
            .on_conflict_do_nothing(
                index_elements=[DiskTable.workspace_id, DiskTable.name],
                index_where=DiskTable.deleted_at.is_(None),
            )
            .returning(DiskTable)
        )
        if created is not None:
            return disk_from_table(created, snapshot_bytes=0), True
        self.session.execute(
            update(DiskTable)
            .where(
                DiskTable.workspace_id == workspace_id,
                DiskTable.name == name,
                DiskTable.deleted_at.is_(None),
                DiskTable.last_stub_id.is_distinct_from(stub_id),
            )
            .values(last_stub_id=stub_id)
        )
        existing = self.get(name, workspace_id=workspace_id)
        if existing is None:
            raise ConflictError("disk changed during creation; retry the request")
        return disk_from_table(existing.row, snapshot_bytes=existing.snapshot_bytes), False

    def get(self, name: str, *, workspace_id: str) -> DiskReading | None:
        result = self.session.execute(
            _with_holder().where(
                DiskTable.workspace_id == workspace_id,
                DiskTable.name == name,
                DiskTable.deleted_at.is_(None),
            )
        ).first()
        return None if result is None else _reading(*result)

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

    def declared_sizes(self, names: list[str], *, workspace_id: str) -> dict[str, int]:
        """The recorded size of each named live disk that exists."""
        if not names:
            return {}
        rows = self.session.execute(
            select(DiskTable.name, DiskTable.size_bytes).where(
                DiskTable.workspace_id == workspace_id,
                DiskTable.name.in_(names),
                DiskTable.deleted_at.is_(None),
            )
        ).tuples()
        return dict(rows.all())

    def placement_hints(
        self, names: list[str], *, workspace_id: str
    ) -> dict[str, DiskPlacementHint]:
        """Where each disk was last held, for placing the container that mounts it next."""
        if not names:
            return {}
        rows = self.session.execute(
            select(
                DiskTable.name,
                DiskTable.last_worker_id,
                DiskTable.volume_zone,
                DiskTable.volume_state,
            ).where(
                DiskTable.workspace_id == workspace_id,
                DiskTable.name.in_(names),
                DiskTable.deleted_at.is_(None),
            )
        ).tuples()
        return {
            name: DiskPlacementHint(
                last_worker_id=last_worker_id,
                volume_zone=volume_zone if volume_state not in ("none", "deleting") else "",
            )
            for name, last_worker_id, volume_zone, volume_state in rows
        }

    def unheld_volume_attachments(self, machine_ids: Sequence[str]) -> dict[str, int]:
        """Disk volumes on each machine that no container placed there reserves.

        A volume takes one of the attachments of the machine it is on, whatever
        state its holder is in: attaching, attached, or still on its way off after
        its holder stopped or moved to another machine. The one volume left out is
        the one whose live holder runs on that same machine, because that
        container's placement already reserved it.
        """
        if not machine_ids:
            return {}
        live = [status.value for status in LIVE_CONTAINER_STATUSES]
        reserved_here = (
            select(ContainerTable.id)
            .outerjoin(WorkerTable, WorkerTable.id == ContainerTable.worker_id)
            .where(
                ContainerTable.id == DiskTable.holder_container_id,
                ContainerTable.status.in_(live),
                or_(
                    ContainerTable.machine_id == ComputeProviderInstanceTable.machine_id,
                    WorkerTable.machine_id == ComputeProviderInstanceTable.machine_id,
                ),
            )
            .exists()
        )
        rows = self.session.execute(
            select(ComputeProviderInstanceTable.machine_id, func.count())
            .select_from(DiskTable)
            .join(
                ComputeProviderInstanceTable,
                ComputeProviderInstanceTable.instance_id == DiskTable.volume_instance_id,
            )
            .where(
                ComputeProviderInstanceTable.machine_id.in_(list(machine_ids)),
                DiskTable.volume_state.in_(_VOLUME_ATTACHED_STATES),
                ~reserved_here,
            )
            .group_by(ComputeProviderInstanceTable.machine_id)
        ).tuples()
        return {str(machine_id): count for machine_id, count in rows}

    def list(self, *, workspace_id: str, after: str, limit: int) -> list[DiskReading]:
        statement = _with_holder().where(
            DiskTable.workspace_id == workspace_id,
            DiskTable.deleted_at.is_(None),
        )
        if after:
            statement = statement.where(DiskTable.name > after)
        rows = self.session.execute(statement.order_by(DiskTable.name).limit(limit)).tuples()
        return [_reading(*row) for row in rows]

    def workloads(self, disk_ids: Sequence[str]) -> dict[str, DiskWorkload]:
        """The workload each disk was last asked for by, while that workload is deployed.

        The stub outlives its deployment, so the workload counts only while a
        live deployment of that app and name exists.
        """
        if not disk_ids:
            return {}
        deployed = (
            select(DeploymentTable.id)
            .where(
                DeploymentTable.app_id == StubTable.app_id,
                DeploymentTable.name == StubTable.name,
                DeploymentTable.kind == DeploymentKind.Pod.value,
                DeploymentTable.deleted_at.is_(None),
            )
            .exists()
        )
        rows = self.session.execute(
            select(DiskTable.id, AppTable.id, AppTable.name, StubTable.name, StubTable.role)
            .join(StubTable, StubTable.id == DiskTable.last_stub_id)
            .join(AppTable, AppTable.id == StubTable.app_id)
            .where(
                DiskTable.id.in_(list(disk_ids)),
                StubTable.type == StubKind.Pod.value,
                AppTable.deleted_at.is_(None),
                deployed,
            )
        ).tuples()
        return {
            str(disk_id): DiskWorkload(
                app_id=str(app_id),
                app_name=app_name,
                name=name,
                role=PodRole(stored_role) if stored_role else PodRole.Service,
            )
            for disk_id, app_id, app_name, name, stored_role in rows
        }

    def lease(self, disk_id: str) -> tuple[str, str] | None:
        """The live disk's holding container and lease token, empty when unheld."""
        row = self.session.execute(
            select(DiskTable.holder_container_id, DiskTable.lease_token).where(
                DiskTable.id == disk_id,
                DiskTable.deleted_at.is_(None),
            )
        ).first()
        if row is None:
            return None
        holder_container_id, lease_token = row
        return str(holder_container_id or ""), lease_token

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

    def due_deletions(self, *, now: datetime, limit: int) -> tuple[DiskDeletionTarget, ...]:
        """Deleting disks whose next attempt is due, the longest due first."""
        rows = self.session.execute(
            select(DiskTable.id, DiskTable.workspace_id)
            .where(DiskTable.deletion_due_at <= now)
            .order_by(DiskTable.deletion_due_at, DiskTable.id)
            .limit(limit)
        ).tuples()
        return tuple(
            DiskDeletionTarget(id=str(disk_id), workspace_id=str(workspace_id))
            for disk_id, workspace_id in rows
        )

    def defer_deletion(
        self, disk_id: str, *, now: datetime, shortest: timedelta, longest: timedelta
    ) -> None:
        """Push a failed deletion's next attempt out by its age so far, within the bounds.

        The wait roughly doubles with each failure, so a deletion that keeps
        failing is tried less and less often and never starves newer ones.
        """
        age = now - DiskTable.deleted_at
        self.session.execute(
            update(DiskTable)
            .where(DiskTable.id == disk_id, DiskTable.deleted_at.is_not(None))
            .values(deletion_due_at=now + func.least(func.greatest(age, shortest), longest))
            .execution_options(synchronize_session=False)
        )

    def workspace_disks(self, workspace_id: str) -> tuple[tuple[str, str, bool], ...]:
        """Every disk of the workspace as (id, name, deleting), deleting ones included."""
        rows = self.session.execute(
            select(DiskTable.id, DiskTable.name, DiskTable.deleted_at.is_not(None))
            .where(DiskTable.workspace_id == workspace_id)
            .order_by(DiskTable.name, DiskTable.id)
        ).tuples()
        return tuple((str(disk_id), name, bool(deleting)) for disk_id, name, deleting in rows)

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
        """The disk's billing position under its row lock.

        Its stored bytes are its chunks and its live volume snapshots together.
        """
        row = self.session.execute(
            select(
                DiskTable.workspace_id,
                DiskTable.name,
                DiskTable.stored_bytes,
                billed_snapshot_bytes().scalar_subquery(),
                DiskTable.metered_bytes,
                DiskTable.metered_at,
                DiskTable.deleted_at,
            )
            .where(DiskTable.id == disk_id)
            .with_for_update(of=DiskTable)
        ).first()
        if row is None:
            return None
        workspace_id, name, stored_bytes, snapshot_bytes, metered_bytes, metered_at, deleted_at = (
            row
        )
        return DiskMeteringCheckpoint(
            id=disk_id,
            workspace_id=str(workspace_id),
            name=name,
            stored_bytes=stored_bytes + snapshot_bytes,
            metered_bytes=metered_bytes,
            metered_at=to_utc(metered_at),
            deleted_at=to_utc(deleted_at) if deleted_at is not None else None,
        )

    def declared_bytes(self, workspace_id: str) -> int:
        """The declared size of every live disk in the workspace, summed."""
        total = self.session.scalar(
            select(func.coalesce(func.sum(DiskTable.size_bytes), 0)).where(
                DiskTable.workspace_id == workspace_id, DiskTable.deleted_at.is_(None)
            )
        )
        return int(total or 0)

    def largest_declared_bytes(self, workspace_ids: list[str]) -> int:
        """The most declared disk size any one of these workspaces holds."""
        if not workspace_ids:
            return 0
        per_workspace = (
            select(func.sum(DiskTable.size_bytes).label("declared"))
            .where(DiskTable.workspace_id.in_(workspace_ids), DiskTable.deleted_at.is_(None))
            .group_by(DiskTable.workspace_id)
            .subquery()
        )
        return int(
            self.session.scalar(select(func.coalesce(func.max(per_workspace.c.declared), 0))) or 0
        )

    def open_attachment(
        self,
        disk_id: str,
        *,
        workspace_id: str,
        container_id: str,
        size_bytes: int,
        at: datetime,
    ) -> None:
        """Start billing a lease at the size the disk has now, ending any lease before it.

        A holder acquiring again keeps the lease it already has.
        """
        held_by = self.session.scalar(
            select(DiskAttachmentTable.container_id).where(
                DiskAttachmentTable.disk_id == disk_id,
                DiskAttachmentTable.released_at.is_(None),
            )
        )
        if held_by is not None and str(held_by) == container_id:
            return
        self.close_attachment(disk_id, at=at)
        self.session.add(
            DiskAttachmentTable(
                disk_id=disk_id,
                workspace_id=workspace_id,
                container_id=container_id,
                size_bytes=size_bytes,
                acquired_at=at,
                metered_at=at,
            )
        )
        self.session.flush()

    def close_attachment(self, disk_id: str, *, at: datetime) -> None:
        self.session.execute(
            update(DiskAttachmentTable)
            .where(
                DiskAttachmentTable.disk_id == disk_id,
                DiskAttachmentTable.released_at.is_(None),
            )
            .values(released_at=func.greatest(DiskAttachmentTable.acquired_at, at))
        )

    def list_attachment_metering_targets(
        self, *, metered_before: datetime, limit: int
    ) -> tuple[tuple[str, str], ...]:
        """Leases with a window due, as (lease, workspace): open ones past the interval,
        released ones at once."""
        rows = self.session.execute(
            select(DiskAttachmentTable.id, DiskAttachmentTable.workspace_id)
            .where(
                DiskAttachmentTable.settled_at.is_(None),
                or_(
                    DiskAttachmentTable.metered_at <= metered_before,
                    DiskAttachmentTable.released_at.is_not(None),
                ),
            )
            .order_by(DiskAttachmentTable.metered_at, DiskAttachmentTable.id)
            .limit(limit)
        ).tuples()
        return tuple(
            (str(attachment_id), str(workspace_id)) for attachment_id, workspace_id in rows
        )

    def unsettled_attachment_ids(self, disk_id: str) -> tuple[str, ...]:
        rows = self.session.scalars(
            select(DiskAttachmentTable.id).where(
                DiskAttachmentTable.disk_id == disk_id,
                DiskAttachmentTable.settled_at.is_(None),
            )
        )
        return tuple(str(attachment_id) for attachment_id in rows)

    def lock_attachment_checkpoint(self, attachment_id: str) -> DiskAttachmentCheckpoint | None:
        row = self.session.execute(
            select(
                DiskAttachmentTable.disk_id,
                DiskAttachmentTable.workspace_id,
                DiskAttachmentTable.container_id,
                DiskAttachmentTable.size_bytes,
                DiskAttachmentTable.metered_at,
                DiskAttachmentTable.released_at,
            )
            .where(
                DiskAttachmentTable.id == attachment_id,
                DiskAttachmentTable.settled_at.is_(None),
            )
            .with_for_update()
        ).first()
        if row is None:
            return None
        disk_id, workspace_id, container_id, size_bytes, metered_at, released_at = row
        finished_at = self.session.scalar(
            select(ContainerTable.finished_at).where(ContainerTable.id == container_id)
        )
        return DiskAttachmentCheckpoint(
            id=attachment_id,
            disk_id=str(disk_id),
            workspace_id=str(workspace_id),
            container_id=str(container_id),
            size_bytes=size_bytes,
            metered_at=to_utc(metered_at),
            released_at=to_utc(released_at) if released_at is not None else None,
            holder_finished_at=to_utc(finished_at) if finished_at is not None else None,
        )

    def advance_attachment(self, attachment_id: str, *, metered_at: datetime) -> None:
        self.session.execute(
            update(DiskAttachmentTable)
            .where(DiskAttachmentTable.id == attachment_id)
            .values(metered_at=metered_at)
        )

    def settle_attachment(self, attachment_id: str, *, ended_at: datetime, at: datetime) -> None:
        """Take a lease billed to its end out of every scan, closing it there if still open."""
        self.session.execute(
            update(DiskAttachmentTable)
            .where(DiskAttachmentTable.id == attachment_id)
            .values(
                metered_at=ended_at,
                released_at=func.coalesce(DiskAttachmentTable.released_at, ended_at),
                settled_at=at,
            )
        )

    def advance_metering_checkpoint(
        self, disk_id: str, *, metered_bytes: int, metered_at: datetime
    ) -> None:
        self.session.execute(
            update(DiskTable)
            .where(DiskTable.id == disk_id)
            .values(metered_bytes=metered_bytes, metered_at=metered_at)
        )


_VOLUME_ATTACHED_STATES = ("attaching", "attached", "releasing", "detaching")
"""Volume states in which the volume holds one of its machine's attachments."""


def _with_holder() -> Select[tuple[DiskTable, str, datetime | None, str, str | None, int]]:
    # An outer join: the container columns are None when the holder row is gone.
    return select(
        DiskTable,
        ContainerTable.status,
        ContainerTable.storage_released_at,
        ContainerTable.runtime_worker_id,
        ContainerTable.worker_id,
        billed_snapshot_bytes().scalar_subquery(),
    ).outerjoin(ContainerTable, ContainerTable.id == DiskTable.holder_container_id)


def _reading(
    row: DiskTable,
    status: str | None,
    storage_released_at: datetime | None,
    runtime_worker_id: str | None,
    worker_id: str | None,
    snapshot_bytes: int,
) -> DiskReading:
    if not row.holder_container_id:
        return DiskReading(row=row, holder=None, snapshot_bytes=snapshot_bytes)
    return DiskReading(
        row=row,
        snapshot_bytes=snapshot_bytes,
        holder=DiskHolder(
            container_id=str(row.holder_container_id),
            status=None if status is None else ContainerStatus(status),
            storage_released=status is None or storage_released_at is not None,
            worker_id=runtime_worker_id or str(worker_id or ""),
        ),
    )


__all__ = [
    "DiskAttachmentCheckpoint",
    "DiskChainLink",
    "DiskDeletionTarget",
    "DiskHolder",
    "DiskMeteringCheckpoint",
    "DiskMeteringTarget",
    "DiskPlacementHint",
    "DiskReading",
    "DiskRepository",
    "disk_from_table",
]
