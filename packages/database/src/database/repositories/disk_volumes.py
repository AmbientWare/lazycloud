from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from database.tables.compute import ComputeProviderInstanceTable, ComputeUnitTable
from database.tables.disks import DiskTable
from database.tables.orchestration import ContainerTable, WorkerTable
from foundation.ids import try_uuid
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.timestamps import to_utc
from sqlalchemy import Text, and_, cast, func, or_, select, update
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class DiskVolumeHost:
    """The provider machine a worker runs on, as far as its disk volumes need to know."""

    workspace_id: str
    """A workspace the machine's provider resolves through."""

    provider_ref: str
    connection_id: str | None
    region: str
    zone: str
    instance_id: str


@dataclass(frozen=True, slots=True)
class DiskVolumeSnapshot:
    disk_id: str
    workspace_id: str
    size_bytes: int
    deleted: bool
    holder_container_id: str
    lease_token: str
    state: str
    volume_id: str
    provider_ref: str
    connection_id: str | None
    region: str
    zone: str
    instance_id: str
    volume_size_bytes: int
    token: str
    formatted: bool
    revision: int
    changed_at: datetime | None


@dataclass(frozen=True, slots=True)
class DiskVolumeScopeRow:
    workspace_id: str
    provider_ref: str
    region: str


_COLUMNS = (
    DiskTable.id,
    DiskTable.workspace_id,
    DiskTable.size_bytes,
    DiskTable.deleted_at,
    DiskTable.holder_container_id,
    DiskTable.lease_token,
    DiskTable.volume_state,
    DiskTable.volume_id,
    DiskTable.volume_provider_ref,
    DiskTable.volume_connection_id,
    DiskTable.volume_region,
    DiskTable.volume_zone,
    DiskTable.volume_instance_id,
    DiskTable.volume_size_bytes,
    DiskTable.volume_token,
    DiskTable.volume_formatted,
    DiskTable.volume_revision,
    DiskTable.volume_changed_at,
)


@dataclass(slots=True)
class DiskVolumeRepository:
    session: Session

    def snapshot(self, disk_id: str) -> DiskVolumeSnapshot | None:
        row = self.session.execute(select(*_COLUMNS).where(DiskTable.id == disk_id)).first()
        if row is None:
            return None
        (
            identity,
            workspace_id,
            size_bytes,
            deleted_at,
            holder,
            lease_token,
            state,
            volume_id,
            provider_ref,
            connection_id,
            region,
            zone,
            instance_id,
            volume_size_bytes,
            token,
            formatted,
            revision,
            changed_at,
        ) = row
        return DiskVolumeSnapshot(
            disk_id=str(identity),
            workspace_id=str(workspace_id),
            size_bytes=size_bytes,
            deleted=deleted_at is not None,
            holder_container_id=str(holder or ""),
            lease_token=lease_token,
            state=state,
            volume_id=volume_id,
            provider_ref=provider_ref,
            connection_id=str(connection_id) if connection_id is not None else None,
            region=region,
            zone=zone,
            instance_id=instance_id,
            volume_size_bytes=volume_size_bytes,
            token=token,
            formatted=formatted,
            revision=revision,
            changed_at=to_utc(changed_at) if changed_at is not None else None,
        )

    def transition(
        self,
        snapshot: DiskVolumeSnapshot,
        *,
        at: datetime,
        state: str,
        volume_id: str | None = None,
        provider_ref: str | None = None,
        connection_id: str | None = None,
        region: str | None = None,
        zone: str | None = None,
        instance_id: str | None = None,
        volume_size_bytes: int | None = None,
        token: str | None = None,
        formatted: bool | None = None,
    ) -> bool:
        """Move the volume on from exactly the state `snapshot` read.

        Fenced on the revision and the lease: a writer that read an older state,
        or read it under a lease that has since changed hands, changes nothing.
        """
        values: dict[str, object] = {
            "volume_state": state,
            "volume_revision": snapshot.revision + 1,
            "volume_changed_at": at,
        }
        if state == "none":
            values |= {
                "volume_id": "",
                "volume_connection_id": None,
                "volume_instance_id": "",
                "volume_size_bytes": 0,
                "volume_token": "",
                "volume_formatted": False,
            }
        for column, value in (
            ("volume_id", volume_id),
            ("volume_provider_ref", provider_ref),
            ("volume_region", region),
            ("volume_zone", zone),
            ("volume_instance_id", instance_id),
            ("volume_size_bytes", volume_size_bytes),
            ("volume_token", token),
            ("volume_formatted", formatted),
        ):
            if value is not None:
                values[column] = value
        if connection_id is not None:
            values["volume_connection_id"] = connection_id
        moved = self.session.scalar(
            update(DiskTable)
            .where(
                DiskTable.id == snapshot.disk_id,
                DiskTable.volume_revision == snapshot.revision,
                DiskTable.lease_token == snapshot.lease_token,
            )
            .values(values)
            .returning(DiskTable.id)
            .execution_options(synchronize_session=False)
        )
        return moved is not None

    def host_for_worker(self, worker_id: str) -> DiskVolumeHost | None:
        """The provider machine under a worker; None for a machine no provider launched."""
        if try_uuid(worker_id) is None:
            return None
        row = self.session.execute(
            select(
                ComputeUnitTable.workspace_id,
                ComputeUnitTable.provider_ref,
                ComputeUnitTable.provider_connection_id,
                ComputeProviderInstanceTable.region,
                ComputeProviderInstanceTable.availability_zone,
                ComputeProviderInstanceTable.instance_id,
            )
            .select_from(WorkerTable)
            .join(
                ComputeProviderInstanceTable,
                ComputeProviderInstanceTable.machine_id == WorkerTable.machine_id,
            )
            .join(ComputeUnitTable, ComputeUnitTable.id == ComputeProviderInstanceTable.pool_id)
            .where(WorkerTable.id == worker_id)
        ).first()
        if row is None:
            return None
        workspace_id, provider_ref, connection_id, region, zone, instance_id = row
        return DiskVolumeHost(
            workspace_id=str(workspace_id),
            provider_ref=provider_ref,
            connection_id=str(connection_id) if connection_id is not None else None,
            region=region,
            zone=zone,
            instance_id=instance_id or "",
        )

    def due(
        self,
        *,
        cached_before: datetime,
        released_before: datetime,
        stuck_before: datetime,
        limit: int,
    ) -> tuple[str, ...]:
        """Disks whose volume has work waiting: one indexed read, idle or not.

        A cached volume is due when its window has passed, a released one after
        the grace its worker has to unmount it, and one in any other transition
        once whoever drove it has plainly stopped. An attached volume is due only
        when the container holding it has finished.
        """
        live = [status.value for status in LIVE_CONTAINER_STATUSES]
        rows = self.session.scalars(
            select(DiskTable.id)
            .outerjoin(ContainerTable, ContainerTable.id == DiskTable.holder_container_id)
            .where(
                DiskTable.volume_state.in_(
                    (
                        "creating",
                        "attaching",
                        "attached",
                        "releasing",
                        "detaching",
                        "cached",
                        "deleting",
                    )
                ),
                or_(
                    and_(
                        DiskTable.volume_state == "cached",
                        DiskTable.volume_changed_at <= cached_before,
                    ),
                    and_(
                        DiskTable.volume_state == "releasing",
                        DiskTable.volume_changed_at <= released_before,
                    ),
                    and_(
                        DiskTable.volume_state.in_(
                            ("creating", "attaching", "detaching", "deleting")
                        ),
                        DiskTable.volume_changed_at <= stuck_before,
                    ),
                    and_(
                        DiskTable.volume_state.in_(("attaching", "attached")),
                        or_(
                            DiskTable.holder_container_id.is_(None),
                            ContainerTable.id.is_(None),
                            ContainerTable.status.not_in(live),
                        ),
                    ),
                ),
            )
            .order_by(DiskTable.volume_changed_at, DiskTable.id)
            .limit(limit)
        )
        return tuple(str(disk_id) for disk_id in rows)

    def scopes(self) -> tuple[DiskVolumeScopeRow, ...]:
        """Every account and region a disk volume was made in, with a workspace to resolve it."""
        rows = self.session.execute(
            select(
                DiskTable.volume_provider_ref,
                DiskTable.volume_region,
                func.min(cast(DiskTable.workspace_id, Text)),
            )
            .where(DiskTable.volume_provider_ref != "")
            .group_by(DiskTable.volume_provider_ref, DiskTable.volume_region)
        ).tuples()
        return tuple(
            DiskVolumeScopeRow(workspace_id=workspace_id, provider_ref=provider_ref, region=region)
            for provider_ref, region, workspace_id in rows
        )

    def recorded(self, disk_ids: Sequence[str]) -> tuple[dict[str, str], frozenset[str]]:
        """For these disks: the volume each row names, and those creating one."""
        if not disk_ids:
            return {}, frozenset()
        rows = self.session.execute(
            select(DiskTable.id, DiskTable.volume_id, DiskTable.volume_state).where(
                DiskTable.id.in_(list(disk_ids))
            )
        ).tuples()
        recorded: dict[str, str] = {}
        creating: set[str] = set()
        for disk_id, volume_id, state in rows:
            recorded[str(disk_id)] = volume_id
            if state == "creating":
                creating.add(str(disk_id))
        return recorded, frozenset(creating)

    def connection_holds_volumes(self, connection_id: str) -> bool:
        return (
            self.session.scalar(
                select(DiskTable.id).where(DiskTable.volume_connection_id == connection_id).limit(1)
            )
            is not None
        )


__all__ = [
    "DiskVolumeHost",
    "DiskVolumeRepository",
    "DiskVolumeScopeRow",
    "DiskVolumeSnapshot",
]
