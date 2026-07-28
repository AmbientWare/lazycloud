from __future__ import annotations

from datetime import UTC, datetime, timedelta

from api.server.services import ApiServices
from database.repositories.observability import UsageRepository
from database.tables.storage import VolumeTable
from scheduler.service import Scheduler, SchedulerMaintenanceControls
from shared.usage import UsageMetric
from sqlalchemy import select
from storage.volume_filesystem import VolumeNamespace
from storage.volume_metering import PersistentVolumeMeteringService


def test_scheduler_meters_volumes_even_when_workload_loops_are_disabled(
    isolated_services: ApiServices,
) -> None:
    # Persistent volumes accrue cost whether or not any workload is running, so
    # a scheduler with its workload loops switched off must still bill for them.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    metered_at = now - timedelta(seconds=120)
    payload = b"persistent-volume-payload"
    record = isolated_services.volumes.create("metered-while-idle")
    with isolated_services.context.database.session() as session:
        row = session.scalars(select(VolumeTable).where(VolumeTable.name == record.name)).one()
        row.size_bytes = len(payload)
        row.metered_at = metered_at
        workspace_id = str(row.workspace_id)
    filesystem = isolated_services.volume_filesystem
    namespace = VolumeNamespace(workspace_id=workspace_id, volume_id=record.id)
    filesystem.ensure_volume(namespace)
    filesystem.write_path(namespace, "payload.bin", (payload,))

    result = Scheduler(
        isolated_services,
        maintenance=SchedulerMaintenanceControls(
            volume_metering=PersistentVolumeMeteringService(
                isolated_services.context,
                filesystem,
            )
        ),
    ).run_once(
        now=now,
        include_cron_jobs=False,
        include_containers=False,
    )

    assert result.volume_metering_count == 1
    assert result.volume_metering_failure_count == 0
    with isolated_services.context.database.session() as session:
        metered = [
            item
            for item in UsageRepository(session).list(workspace_id=workspace_id)
            if item.metric is UsageMetric.PersistentVolumeByteSeconds
        ]
        checkpoint = session.scalars(
            select(VolumeTable).where(VolumeTable.name == record.name)
        ).one()
    assert [item.quantity for item in metered] == [len(payload) * 120]
    assert checkpoint.metered_at.replace(tzinfo=UTC) == now
