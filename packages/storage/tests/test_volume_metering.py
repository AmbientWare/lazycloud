from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from api.server.services import ApiServices
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.observability import UsageRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.storage import VolumeTable
from shared.billing_quotes import BilledDimension, LedgerComponent
from shared.timestamps import utc_now
from shared.usage import (
    METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY,
    METERING_OBSERVATION_QUALITY_METADATA_KEY,
    MeteringObservationQuality,
    UsageMetric,
    UsageUnit,
)
from sqlalchemy import select
from storage.volume_filesystem import LocalVolumeFilesystem, VolumeNamespace
from storage.volume_metering import PersistentVolumeMeteringService

from storage import volume_metering

# Ten tebibytes held for an hour and a millisecond: past the range a binary float
# holds every integer in, and not a figure one can represent at all. A window that
# long is what a stalled metering loop leaves behind, and the volume is a size the
# product places no cap below.
_LARGE_SIZE_BYTES = 10 * 2**40
_LARGE_WINDOW = timedelta(hours=1, milliseconds=1)
_LARGE_BYTE_SECONDS = Decimal("39582429595052277.76")


def test_volume_metering_records_byte_seconds_and_advances_checkpoint(
    isolated_services: ApiServices,
) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    observed_at = started_at + _LARGE_WINDOW
    finished_at = observed_at + timedelta(seconds=5)
    record = isolated_services.volumes.create("metered-data")
    workspace_id = _set_checkpoint(
        isolated_services,
        volume_name=record.name,
        size_bytes=_LARGE_SIZE_BYTES,
        metered_at=started_at,
    )
    payload = b"persistent-volume-payload"
    filesystem = isolated_services.volume_filesystem
    namespace = VolumeNamespace(workspace_id=workspace_id, volume_id=record.id)
    filesystem.ensure_volume(namespace)
    filesystem.write_path(namespace, "nested/payload.bin", (payload,))
    metering = PersistentVolumeMeteringService(isolated_services.context, filesystem)

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
    with isolated_services.context.database.session() as session:
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
    isolated_services: ApiServices,
) -> None:
    first = isolated_services.volumes.create("first")
    second = isolated_services.volumes.create("second")
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    workspace_id = _set_checkpoint(
        isolated_services,
        volume_name=first.name,
        size_bytes=3,
        metered_at=started_at,
    )
    filesystem = isolated_services.volume_filesystem
    first_namespace = VolumeNamespace(workspace_id, first.id)
    second_namespace = VolumeNamespace(workspace_id, second.id)
    filesystem.write_path(first_namespace, "a.bin", (b"1234",))
    filesystem.write_path(second_namespace, "unrelated.bin", (b"x" * 100,))

    result = PersistentVolumeMeteringService(
        isolated_services.context,
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
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = isolated_services.volumes.create("degraded-final")
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    observed_at = started_at + timedelta(seconds=5)
    workspace_id = _set_checkpoint(
        isolated_services,
        volume_name=record.name,
        size_bytes=7,
        metered_at=started_at,
    )
    filesystem = _FailingOccupancyFilesystem(
        isolated_services.context.paths.root / "unavailable-volumes"
    )
    metering = PersistentVolumeMeteringService(isolated_services.context, filesystem)
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
    with isolated_services.context.database.session() as session:
        checkpoint = session.scalars(
            select(VolumeTable).where(VolumeTable.name == record.name)
        ).one()
    assert checkpoint.size_bytes == 7
    assert checkpoint.metered_at.replace(tzinfo=UTC) == observed_at


def test_a_metered_volume_window_is_priced_in_the_transaction_that_records_it(
    isolated_services: ApiServices,
) -> None:
    """Volume storage is a billed dimension, so metering it owes a cost.

    A window recorded without one is money this platform measured and can no
    longer charge for, and the usage row alone cannot say afterwards whether the
    cost was skipped or was never owed. Priced at the published zero here, which
    is the case that would be easiest to leave unwired and hardest to notice: the
    ledger row and the invoice line still have to exist to say the storage is
    measured and free.
    """

    now = utc_now()
    started_at = now + timedelta(minutes=2)
    observed_at = started_at + timedelta(seconds=10)
    record = isolated_services.volumes.create("priced-data")
    workspace_id = _set_checkpoint(
        isolated_services,
        volume_name=record.name,
        size_bytes=2_048,
        metered_at=started_at,
    )
    with isolated_services.context.database.session() as session:
        PlatformRateRepository(session).publish(
            pricing_version="test.a",
            effective_at=now + timedelta(minutes=1),
            nanos_per_egress_byte=Decimal(0),
            nanos_per_volume_byte_second=Decimal(0),
        )

    result = PersistentVolumeMeteringService(
        isolated_services.context,
        isolated_services.volume_filesystem,
    ).reconcile_volume(record.name, workspace_id=workspace_id, now=observed_at)

    assert result is not None
    with isolated_services.context.database.session() as session:
        segments = list(
            session.scalars(
                select(BillingLedgerSegmentTable).where(
                    BillingLedgerSegmentTable.usage_record_id == result.usage_record.id
                )
            )
        )
    assert len(segments) == 1
    assert segments[0].dimension == BilledDimension.VolumeStorage.value
    assert segments[0].rate_nanos_per_unit == Decimal(0)
    assert segments[0].cost_nanos == 0
    assert segments[0].component == LedgerComponent.VolumeStorage.value
    assert segments[0].quantity == Decimal(result.byte_seconds)


class _FailingOccupancyFilesystem(LocalVolumeFilesystem):
    def occupancy_bytes(self, namespace: VolumeNamespace) -> int:
        raise TimeoutError(f"timed out scanning {namespace.volume_id}")


def _set_checkpoint(
    services: ApiServices,
    *,
    volume_name: str,
    size_bytes: int,
    metered_at: datetime,
) -> str:
    with services.context.database.session() as session:
        row = session.scalars(select(VolumeTable).where(VolumeTable.name == volume_name)).one()
        row.size_bytes = size_bytes
        row.metered_at = metered_at
        workspace_id = str(row.workspace_id)
    return workspace_id
