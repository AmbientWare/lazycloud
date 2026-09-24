from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from database.tables.compute import ComputeProviderInstanceTable, ComputeUnitTable
from database.tables.disks import DiskSnapshotTable, DiskTable, DiskVolumeOrphanTable
from database.tables.orchestration import ContainerTable, WorkerTable
from foundation.ids import try_uuid
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.timestamps import to_utc
from sqlalchemy import and_, case, delete, func, or_, select, union, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class DiskVolumeHost:
    """The provider machine a worker runs on, as far as its disk volumes need to know."""

    workspace_id: str
    """The workspace the machine's capacity belongs to; the provider resolves through it."""

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
    capacity_workspace_id: str
    region: str
    zone: str
    instance_id: str
    volume_size_bytes: int
    token: str
    formatted: bool
    revision: int
    driver: str
    changed_at: datetime | None
    driven_at: datetime | None
    source_snapshot_id: str


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
    DiskTable.volume_capacity_workspace_id,
    DiskTable.volume_region,
    DiskTable.volume_zone,
    DiskTable.volume_instance_id,
    DiskTable.volume_size_bytes,
    DiskTable.volume_token,
    DiskTable.volume_formatted,
    DiskTable.volume_revision,
    DiskTable.volume_driver,
    DiskTable.volume_changed_at,
    DiskTable.volume_driven_at,
    DiskTable.volume_source_snapshot_id,
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
            capacity_workspace_id,
            region,
            zone,
            instance_id,
            volume_size_bytes,
            token,
            formatted,
            revision,
            driver,
            changed_at,
            driven_at,
            source_snapshot_id,
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
            capacity_workspace_id=capacity_workspace_id,
            region=region,
            zone=zone,
            instance_id=instance_id,
            volume_size_bytes=volume_size_bytes,
            token=token,
            formatted=formatted,
            revision=revision,
            driver=driver,
            changed_at=to_utc(changed_at) if changed_at is not None else None,
            driven_at=to_utc(driven_at) if driven_at is not None else None,
            source_snapshot_id=source_snapshot_id,
        )

    def transition(
        self,
        snapshot: DiskVolumeSnapshot,
        *,
        at: datetime,
        state: str,
        driver: str,
        volume_id: str | None = None,
        provider_ref: str | None = None,
        connection_id: str | None = None,
        capacity_workspace_id: str | None = None,
        region: str | None = None,
        zone: str | None = None,
        instance_id: str | None = None,
        volume_size_bytes: int | None = None,
        token: str | None = None,
        formatted: bool | None = None,
        source_snapshot_id: str | None = None,
        performed: bool = False,
    ) -> bool:
        """Move the volume on from exactly the state `snapshot` read.

        A decision is fenced on the revision and the lease. A writer that read an
        older state, or read it under a lease that has since changed hands,
        changes nothing. The result of a provider call the snapshot's driver
        already made (`performed`) is fenced on the revision and that driver
        instead, because the provider has changed whatever the lease says now,
        and only a record of it lets the next holder continue from the truth.
        """
        values: dict[str, object] = {
            "volume_state": state,
            "volume_revision": snapshot.revision + 1,
            "volume_driver": driver,
            "volume_changed_at": at,
            "volume_driven_at": at,
        }
        if state == "none":
            values |= {
                "volume_id": "",
                "volume_connection_id": None,
                "volume_instance_id": "",
                "volume_size_bytes": 0,
                "volume_token": "",
                "volume_formatted": False,
                "volume_source_snapshot_id": "",
            }
        for column, value in (
            ("volume_id", volume_id),
            ("volume_provider_ref", provider_ref),
            ("volume_capacity_workspace_id", capacity_workspace_id),
            ("volume_region", region),
            ("volume_zone", zone),
            ("volume_instance_id", instance_id),
            ("volume_size_bytes", volume_size_bytes),
            ("volume_token", token),
            ("volume_formatted", formatted),
            ("volume_source_snapshot_id", source_snapshot_id),
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
                DiskTable.volume_driver == snapshot.driver
                if performed
                else DiskTable.lease_token == snapshot.lease_token,
            )
            .values(values)
            .returning(DiskTable.id)
            .execution_options(synchronize_session=False)
        )
        return moved is not None

    def claim(self, snapshot: DiskVolumeSnapshot, *, at: datetime) -> DiskVolumeSnapshot | None:
        """Renew the snapshot's driver's hold on its state just before a provider call.

        Fenced on the revision, driver and lease the snapshot read, and moves the
        revision, so a takeover that read the driver as silent changes nothing
        once the driver has claimed again. None when the state is no longer the
        driver's to call for.
        """
        moved = self.session.scalar(
            update(DiskTable)
            .where(
                DiskTable.id == snapshot.disk_id,
                DiskTable.volume_revision == snapshot.revision,
                DiskTable.volume_driver == snapshot.driver,
                DiskTable.lease_token == snapshot.lease_token,
            )
            .values(volume_revision=snapshot.revision + 1, volume_driven_at=at)
            .returning(DiskTable.id)
            .execution_options(synchronize_session=False)
        )
        if moved is None:
            return None
        return replace(snapshot, revision=snapshot.revision + 1, driven_at=at)

    def defer(self, snapshot: DiskVolumeSnapshot, *, at: datetime) -> None:
        """Push an unchanged volume to the back of the due order without moving it.

        Only the due ordering moves; the driver's claim time does not, so a
        deferred volume whose driver died is still taken over.
        """
        self.session.execute(
            update(DiskTable)
            .where(
                DiskTable.id == snapshot.disk_id,
                DiskTable.volume_revision == snapshot.revision,
                DiskTable.lease_token == snapshot.lease_token,
            )
            .values(volume_changed_at=at)
            .execution_options(synchronize_session=False)
        )

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
        lease_driver_prefix: str,
        lease_silent_before: datetime,
        sweep_silent_before: datetime,
        recheck_before: datetime,
        limit: int,
    ) -> tuple[str, ...]:
        """Disks whose volume has work waiting, in one indexed read whether idle or not.

        A cached volume is due when its window has passed, a released one after
        the grace its worker has to unmount it, and one in any other transition
        once whoever drove it has been silent too long: a driver named with
        `lease_driver_prefix` since `lease_silent_before`, any other since
        `sweep_silent_before`. An attached volume is due when
        its holder is gone, or finished with its storage released. A finished
        holder whose storage is not released may still publish, so its volume is
        only rechecked once `recheck_before` has passed since it was last looked
        at; the sweep pushes it back each time it finds the holder still keeps it.
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
                        DiskTable.volume_driven_at
                        <= case(
                            (
                                DiskTable.volume_driver.startswith(lease_driver_prefix),
                                lease_silent_before,
                            ),
                            else_=sweep_silent_before,
                        ),
                    ),
                    and_(
                        DiskTable.volume_state.in_(("attaching", "attached")),
                        or_(
                            DiskTable.holder_container_id.is_(None),
                            ContainerTable.id.is_(None),
                            and_(
                                ContainerTable.status.not_in(live),
                                or_(
                                    ContainerTable.storage_released_at.is_not(None),
                                    DiskTable.volume_changed_at <= recheck_before,
                                ),
                            ),
                        ),
                    ),
                ),
            )
            .order_by(DiskTable.volume_changed_at, DiskTable.id)
            .limit(limit)
        )
        return tuple(str(disk_id) for disk_id in rows)

    def scopes(self) -> tuple[DiskVolumeScopeRow, ...]:
        """Every account and region a disk volume was made in, with a workspace to resolve it.

        Abandoned creations count: their disk may be gone, but the volume they
        may have made is still in that account until collection removes it.
        """
        disks = (
            select(
                DiskTable.volume_provider_ref.label("provider_ref"),
                DiskTable.volume_region.label("region"),
                DiskTable.volume_capacity_workspace_id.label("workspace_id"),
            )
            .where(
                DiskTable.volume_provider_ref != "",
                DiskTable.volume_capacity_workspace_id != "",
            )
            .distinct()
        )
        orphans = select(
            DiskVolumeOrphanTable.provider_ref,
            DiskVolumeOrphanTable.region,
            DiskVolumeOrphanTable.capacity_workspace_id,
        ).distinct()
        every = union(disks, orphans).subquery()
        rows = self.session.execute(
            select(every.c.provider_ref, every.c.region, func.min(every.c.workspace_id)).group_by(
                every.c.provider_ref, every.c.region
            )
        ).tuples()
        return tuple(
            DiskVolumeScopeRow(workspace_id=workspace_id, provider_ref=provider_ref, region=region)
            for provider_ref, region, workspace_id in rows
        )

    def record_orphan(self, snapshot: DiskVolumeSnapshot) -> None:
        """Keep the creation `snapshot` names for collection, in case it made a volume."""
        self.session.execute(
            postgresql_insert(DiskVolumeOrphanTable)
            .values(
                disk_id=snapshot.disk_id,
                workspace_id=snapshot.workspace_id,
                provider_ref=snapshot.provider_ref,
                connection_id=snapshot.connection_id,
                capacity_workspace_id=snapshot.capacity_workspace_id,
                region=snapshot.region,
                creation_token=snapshot.token,
            )
            .on_conflict_do_nothing(index_elements=[DiskVolumeOrphanTable.creation_token])
        )

    def orphan_tokens(self, *, provider_ref: str, region: str) -> dict[str, datetime]:
        """The abandoned creation tokens in one account and region, with when each was kept."""
        rows = self.session.execute(
            select(DiskVolumeOrphanTable.creation_token, DiskVolumeOrphanTable.created_at).where(
                DiskVolumeOrphanTable.provider_ref == provider_ref,
                DiskVolumeOrphanTable.region == region,
            )
        ).tuples()
        return {token: to_utc(created_at) for token, created_at in rows}

    def settle_orphans(self, tokens: Sequence[str]) -> None:
        if tokens:
            self.session.execute(
                delete(DiskVolumeOrphanTable).where(
                    DiskVolumeOrphanTable.creation_token.in_(list(tokens))
                )
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
        """Whether a disk volume or snapshot, or a creation that may have made a volume, is in
        this account."""
        held = select(DiskTable.id).where(DiskTable.volume_connection_id == connection_id)
        pending = select(DiskVolumeOrphanTable.id).where(
            DiskVolumeOrphanTable.connection_id == connection_id
        )
        snapshots = select(DiskSnapshotTable.id).where(
            DiskSnapshotTable.connection_id == connection_id
        )
        return bool(
            self.session.scalar(select(or_(held.exists(), pending.exists(), snapshots.exists())))
        )


__all__ = [
    "DiskVolumeHost",
    "DiskVolumeRepository",
    "DiskVolumeScopeRow",
    "DiskVolumeSnapshot",
]
