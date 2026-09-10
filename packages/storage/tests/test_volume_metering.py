from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from database.context import ServiceContext
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.observability import UsageRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.storage import VolumeTable
from execution.volumes.records import VolumeService
from shared.billing_quotes import BilledDimension, LedgerComponent
from shared.billing_rate_card import PUBLISHED_METERED_RATE_HISTORY
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
    filesystem = LocalVolumeFilesystem(service_context.paths.root / "volumes")
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
    filesystem = LocalVolumeFilesystem(service_context.paths.root / "volumes")
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
    filesystem = _FailingOccupancyFilesystem(service_context.paths.root / "unavailable-volumes")
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
        LocalVolumeFilesystem(service_context.paths.root / "volumes"),
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


class _FailingOccupancyFilesystem(LocalVolumeFilesystem):
    def occupancy_bytes(self, namespace: VolumeNamespace) -> int:
        raise TimeoutError(f"timed out scanning {namespace.volume_id}")


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
