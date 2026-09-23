from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.context import ServiceContext
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.observability import UsageRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.disks import DiskAttachmentTable
from database.tables.orchestration import ContainerTable
from database.tables.storage import VolumeTable
from execution.volumes.records import VolumeService
from shared.billing_quotes import BilledDimension, LedgerComponent
from shared.billing_rate_card import PUBLISHED_DISK_RATE, PUBLISHED_METERED_RATE_HISTORY
from shared.containers import ContainerRecord, ContainerStatus
from shared.disks import DiskMount
from shared.timestamps import utc_now
from shared.usage import (
    METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY,
    METERING_OBSERVATION_QUALITY_METADATA_KEY,
    MeteringObservationQuality,
    UsageMetric,
    UsageUnit,
)
from sqlalchemy import select
from storage.disks import get_or_create_disks
from storage.volume_filesystem import (
    VolumeNamespace,
    WorkspaceVolumeFilesystem,
    workspace_volume_store_resolver,
)
from storage.volume_metering import PersistentVolumeMeteringService
from tests.fakes import FakeObjectClient, FakeWorkspaceStorageIssuer
from tests.workspaces import on_team_plan

from storage import volume_metering

# This quantity exceeds float integer precision and protects exact billing.
_LARGE_SIZE_BYTES = 10 * 2**40
_LARGE_WINDOW = timedelta(hours=1, milliseconds=1)
_LARGE_BYTE_SECONDS = Decimal("39582429595052277.76")


def test_volume_metering_records_byte_seconds_and_advances_checkpoint(
    service_context: ServiceContext,
) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    observed_at = started_at + _LARGE_WINDOW
    finished_at = observed_at + timedelta(seconds=5)
    record = VolumeService(service_context).get_or_create("metered-data", admit=None)
    workspace_id = _set_checkpoint(
        service_context,
        volume_name=record.name,
        size_bytes=_LARGE_SIZE_BYTES,
        metered_at=started_at,
    )
    payload = b"persistent-volume-payload"
    filesystem = WorkspaceVolumeFilesystem(
        workspace_volume_store_resolver(
            service_context.database,
            object_store=FakeObjectClient(),
            storage_issuer=FakeWorkspaceStorageIssuer(),
        )
    )
    namespace = VolumeNamespace(workspace_id=workspace_id, volume_id=record.id)
    filesystem.ensure_volume(namespace)
    filesystem.write_path(namespace, "nested/payload.bin", (payload,))
    metering = PersistentVolumeMeteringService(service_context, filesystem)

    initial = metering.reconcile_volume(
        record.name,
        workspace_id=workspace_id,
        now=observed_at,
    )
    measured = metering.reconcile_volume(
        record.name,
        workspace_id=workspace_id,
        now=finished_at,
    )
    duplicate = metering.reconcile_volume(
        record.name,
        workspace_id=workspace_id,
        now=finished_at,
    )

    assert initial is not None
    assert initial.previous_size_bytes == _LARGE_SIZE_BYTES
    assert initial.observed_size_bytes == len(payload)
    assert initial.byte_seconds == _LARGE_BYTE_SECONDS
    assert measured is not None
    assert measured.byte_seconds == len(payload) * 5
    assert measured.usage_record.metric is UsageMetric.PersistentVolumeByteSeconds
    assert measured.usage_record.unit is UsageUnit.ByteSeconds
    assert measured.usage_record.metadata["previous_size_bytes"] == len(payload)
    assert duplicate is None
    with service_context.database.session() as session:
        records = UsageRepository(session).list(workspace_id=workspace_id)
        checkpoint = session.scalars(
            select(VolumeTable).where(VolumeTable.name == record.name)
        ).one()
    assert (
        len([item for item in records if item.metric is UsageMetric.PersistentVolumeByteSeconds])
        == 2
    )
    assert checkpoint.size_bytes == len(payload)
    assert checkpoint.metered_at.replace(tzinfo=UTC) == finished_at


def test_volume_metering_scans_only_the_stable_volume_namespace(
    service_context: ServiceContext,
) -> None:
    first = VolumeService(service_context).get_or_create("first", admit=None)
    second = VolumeService(service_context).get_or_create("second", admit=None)
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    workspace_id = _set_checkpoint(
        service_context,
        volume_name=first.name,
        size_bytes=3,
        metered_at=started_at,
    )
    filesystem = WorkspaceVolumeFilesystem(
        workspace_volume_store_resolver(
            service_context.database,
            object_store=FakeObjectClient(),
            storage_issuer=FakeWorkspaceStorageIssuer(),
        )
    )
    first_namespace = VolumeNamespace(workspace_id, first.id)
    second_namespace = VolumeNamespace(workspace_id, second.id)
    filesystem.write_path(first_namespace, "a.bin", (b"1234",))
    filesystem.write_path(second_namespace, "unrelated.bin", (b"x" * 100,))

    result = PersistentVolumeMeteringService(
        service_context,
        filesystem,
    ).reconcile_volume(
        first.name,
        workspace_id=workspace_id,
        now=started_at + timedelta(seconds=3),
    )

    assert result is not None
    assert result.observed_size_bytes == 4
    assert result.byte_seconds == 9
    assert result.usage_record.metadata[METERING_OBSERVATION_QUALITY_METADATA_KEY] == (
        MeteringObservationQuality.Authoritative.value
    )


def test_final_volume_metering_closes_checkpoint_window_when_scan_fails(
    service_context: ServiceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = VolumeService(service_context).get_or_create("degraded-final", admit=None)
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    observed_at = started_at + timedelta(seconds=5)
    workspace_id = _set_checkpoint(
        service_context,
        volume_name=record.name,
        size_bytes=7,
        metered_at=started_at,
    )
    filesystem = WorkspaceVolumeFilesystem(
        workspace_volume_store_resolver(
            service_context.database,
            object_store=FakeObjectClient(),
            storage_issuer=FakeWorkspaceStorageIssuer(),
        )
    )

    def unavailable(self: WorkspaceVolumeFilesystem, namespace: VolumeNamespace) -> int:
        raise TimeoutError(f"timed out scanning {namespace.volume_id}")

    monkeypatch.setattr(WorkspaceVolumeFilesystem, "occupancy_bytes", unavailable)
    metering = PersistentVolumeMeteringService(service_context, filesystem)
    monkeypatch.setattr(volume_metering, "utc_now", lambda: observed_at)

    result = metering.finalize_volume_deletion(
        record.name,
        workspace_id=workspace_id,
    )

    assert result is not None
    assert result.byte_seconds == 35
    assert result.observed_size_bytes is None
    assert result.usage_record.metadata[METERING_OBSERVATION_QUALITY_METADATA_KEY] == (
        MeteringObservationQuality.CheckpointEstimate.value
    )
    assert result.usage_record.metadata[METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY] == (
        "TimeoutError"
    )
    assert "observed_size_bytes" not in result.usage_record.metadata
    with service_context.database.session() as session:
        checkpoint = session.scalars(
            select(VolumeTable).where(VolumeTable.name == record.name)
        ).one()
    assert checkpoint.size_bytes == 7
    assert checkpoint.metered_at.replace(tzinfo=UTC) == observed_at


def test_a_metered_volume_window_prices_byte_seconds_exactly(
    service_context: ServiceContext,
) -> None:
    """Small storage rates retain exact costs over large byte-second quantities."""

    rate = Decimal("0.000000017965")
    now = max(utc_now(), *(card.effective_at for card in PUBLISHED_METERED_RATE_HISTORY))
    started_at = now + timedelta(minutes=2)
    observed_at = started_at + timedelta(hours=1)
    record = VolumeService(service_context).get_or_create("priced-data", admit=None)
    workspace_id = _set_checkpoint(
        service_context,
        volume_name=record.name,
        size_bytes=2**30,
        metered_at=started_at,
    )
    with service_context.database.session() as session:
        PlatformRateRepository(session).publish(
            pricing_version="test.a",
            effective_at=now + timedelta(minutes=1),
            nanos_per_egress_byte=Decimal(0),
            nanos_per_volume_byte_second=rate,
        )

    result = PersistentVolumeMeteringService(
        service_context,
        WorkspaceVolumeFilesystem(
            workspace_volume_store_resolver(
                service_context.database,
                object_store=FakeObjectClient(),
                storage_issuer=FakeWorkspaceStorageIssuer(),
            )
        ),
    ).reconcile_volume(record.name, workspace_id=workspace_id, now=observed_at)

    assert result is not None
    with service_context.database.session() as session:
        segments = list(
            session.scalars(
                select(BillingLedgerSegmentTable).where(
                    BillingLedgerSegmentTable.usage_record_id == result.usage_record.id
                )
            )
        )
    assert len(segments) == 1
    assert segments[0].dimension == BilledDimension.VolumeStorage.value
    assert segments[0].rate_nanos_per_unit == rate
    assert segments[0].cost_nanos == 69_443
    assert segments[0].component == LedgerComponent.VolumeStorage.value
    assert segments[0].quantity == Decimal(result.byte_seconds)


def _set_checkpoint(
    context: ServiceContext,
    *,
    volume_name: str,
    size_bytes: int,
    metered_at: datetime,
) -> str:
    with context.database.session() as session:
        row = session.scalars(select(VolumeTable).where(VolumeTable.name == volume_name)).one()
        row.size_bytes = size_bytes
        row.metered_at = metered_at
        workspace_id = str(row.workspace_id)
    return workspace_id


def test_attached_capacity_bills_each_lease_once_up_to_its_end(
    isolated_services: ApiServices,
) -> None:
    """Declared size times lease time, in windows that tile each lease and stop at its end.

    A lease ends at its release, or when its holder stops without releasing, since
    a stopped container holds nothing a customer can use.
    """

    services = isolated_services
    size_bytes = 2 * 1024**3
    with services.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    on_team_plan(services.database, workspace_id)
    [resolved] = get_or_create_disks(
        services.database,
        [DiskMount(name="box-root", size_bytes=size_bytes)],
        workspace_id=workspace_id,
    )
    disk_id = resolved.record.id
    metering = PersistentVolumeMeteringService(
        services.context,
        WorkspaceVolumeFilesystem(
            workspace_volume_store_resolver(
                services.database,
                object_store=FakeObjectClient(),
                storage_issuer=FakeWorkspaceStorageIssuer(),
            )
        ),
    )

    first = _running_container(services, workspace_id)
    lease = services.disks.acquire(disk_id, container_id=first, worker_id="worker-a")
    acquired_at = utc_now() - timedelta(minutes=10)
    _backdate_open_attachment(services, disk_id, acquired_at)
    checkpoint = acquired_at + timedelta(minutes=5)
    metering.reconcile_due(now=checkpoint)
    metering.reconcile_due(now=checkpoint)
    services.disks.release(disk_id, container_id=first, lease_token=lease.lease_token)
    released_at = _attachments(services, disk_id)[0][0]
    assert released_at is not None
    metering.reconcile_due(now=released_at + timedelta(minutes=5))
    metering.reconcile_due(now=released_at + timedelta(minutes=10))

    second = _running_container(services, workspace_id)
    services.disks.acquire(disk_id, container_id=second, worker_id="worker-b")
    reacquired_at = released_at + timedelta(minutes=1)
    stopped_at = reacquired_at + timedelta(minutes=3)
    _backdate_open_attachment(services, disk_id, reacquired_at)
    with services.database.session() as session:
        row = session.get(ContainerTable, second)
        assert row is not None
        row.status = ContainerStatus.Stopped.value
        row.finished_at = stopped_at
    metering.reconcile_due(now=stopped_at + timedelta(minutes=10))
    metering.reconcile_due(now=stopped_at + timedelta(minutes=20))

    with services.database.session() as session:
        windows = sorted(
            (
                datetime.fromisoformat(str(record.metadata["metering_window_started_at"])),
                datetime.fromisoformat(str(record.metadata["metering_window_ended_at"])),
                record.quantity,
                record.id,
            )
            for record in UsageRepository(session).list(workspace_id=workspace_id)
            if record.metric is UsageMetric.DiskAttachedByteSeconds
        )
        segments = {
            segment.usage_record_id: segment
            for segment in session.scalars(
                select(BillingLedgerSegmentTable).where(
                    BillingLedgerSegmentTable.component == LedgerComponent.DiskAttached.value
                )
            )
        }
    assert [(started, ended) for started, ended, _, _ in windows] == [
        (acquired_at, checkpoint),
        (checkpoint, released_at),
        (reacquired_at, stopped_at),
    ]
    for started, ended, quantity, record_id in windows:
        assert quantity == size_bytes * ((ended - started) // timedelta(milliseconds=1)) / 1000
        segment = segments[record_id]
        assert segment.dimension == BilledDimension.Disk.value
        assert segment.rate_nanos_per_unit == PUBLISHED_DISK_RATE.nanos_per_attached_byte_second
    assert _attachments(services, disk_id) == [(released_at, True), (stopped_at, True)]


def _running_container(services: ApiServices, workspace_id: str) -> str:
    container_id = str(uuid4())
    with services.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="box",
                image="image",
                command=[],
                workspace_id=workspace_id,
                runtime_worker_id="worker",
                status=ContainerStatus.Running,
            )
        )
    return container_id


def _backdate_open_attachment(services: ApiServices, disk_id: str, at: datetime) -> None:
    with services.database.session() as session:
        row = session.scalars(
            select(DiskAttachmentTable).where(
                DiskAttachmentTable.disk_id == disk_id,
                DiskAttachmentTable.released_at.is_(None),
            )
        ).one()
        row.acquired_at = at
        row.metered_at = at


def _attachments(services: ApiServices, disk_id: str) -> list[tuple[datetime | None, bool]]:
    """Each lease's end and whether it is settled, oldest first."""
    with services.database.session() as session:
        rows = session.scalars(
            select(DiskAttachmentTable)
            .where(DiskAttachmentTable.disk_id == disk_id)
            .order_by(DiskAttachmentTable.acquired_at)
        ).all()
        return [
            (
                row.released_at.astimezone(UTC) if row.released_at is not None else None,
                row.settled_at is not None,
            )
            for row in rows
        ]
