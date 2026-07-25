from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from api.server.services import ApiServices
from database.repositories.observability import UsageRepository
from database.tables.storage import VolumeTable
from scheduler.service import Scheduler, SchedulerMaintenanceControls
from shared.usage import (
    METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY,
    METERING_OBSERVATION_QUALITY_METADATA_KEY,
    MeteringObservationQuality,
    UsageMetric,
    UsageUnit,
)
from sqlalchemy import select
from storage.volume_filesystem import LocalVolumeFilesystem, VolumeNamespace
from storage.volume_metering import (
    PersistentVolumeMeteringBatch,
    PersistentVolumeMeteringService,
)

from storage import volume_metering


@dataclass(slots=True)
class _RecordingMeter:
    calls: list[tuple[datetime | None, int]]

    def reconcile_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> PersistentVolumeMeteringBatch:
        self.calls.append((now, limit))
        return PersistentVolumeMeteringBatch(metered_count=2, failure_count=1)


def test_volume_metering_records_byte_seconds_and_advances_checkpoint(
    isolated_services: ApiServices,
) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    observed_at = started_at + timedelta(seconds=10)
    finished_at = observed_at + timedelta(seconds=5)
    record = isolated_services.volumes.create("metered-data")
    workspace_id = _set_checkpoint(
        isolated_services,
        volume_name=record.name,
        size_bytes=0,
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
    assert initial.previous_size_bytes == 0
    assert initial.observed_size_bytes == len(payload)
    assert initial.byte_seconds == 0
    assert measured is not None
    assert measured.byte_seconds == len(payload) * 5
    assert measured.usage_record.metric is UsageMetric.PersistentVolumeByteSeconds
    assert measured.usage_record.unit is UsageUnit.ByteSeconds
    assert measured.usage_record.labels["storage_backend"] == "juicefs"
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


def test_scheduler_meters_volumes_even_when_workload_loops_are_disabled(
    isolated_services: ApiServices,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    meter = _RecordingMeter(calls=[])

    result = Scheduler(
        isolated_services,
        maintenance=SchedulerMaintenanceControls(volume_metering=meter),
    ).run_once(
        now=now,
        include_cron_jobs=False,
        include_containers=False,
        container_limit=17,
    )

    assert result.volume_metering_count == 2
    assert result.volume_metering_failure_count == 1
    assert meter.calls == [(now, 17)]


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
