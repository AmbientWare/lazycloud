from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from api.server.workspace_deletion import WorkspaceDeletionService
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.repositories.compute import TailnetCleanupTombstoneRepository
from database.repositories.identity import WorkspaceRepository
from database.tables.compute import TailnetCleanupTombstoneTable
from database.tailnet_cleanup import DatabaseTailnetCleanupStore
from networking.tailnet_cleanup import TailnetCleanupCoordinator
from networking.tailnet_control import TailnetAuthKey, TailnetDevice, tailnet_machine_hostname
from scheduler.service import (
    Scheduler,
    SchedulerMaintenanceControls,
    UnavailableTailnetCleanupService,
)
from shared.compute_policy import (
    MachinePool,
    UnitName,
)
from shared.identity import WorkspaceStatus
from sqlalchemy import delete
from tests.redis_fakes import FakeRedis
from tests.service_fixtures import administrator_credential

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


class _TailnetControl:
    def __init__(self) -> None:
        self.devices: dict[str, TailnetDevice] = {}
        self.revoked_key_ids: list[str] = []
        self.removed_device_ids: list[str] = []
        self.find_hostnames: list[str] = []

    def issue_auth_key(self, *, machine_id: str, hostname: str) -> TailnetAuthKey:
        raise AssertionError((machine_id, hostname))

    def revoke_auth_key(self, key_id: str) -> None:
        if key_id not in self.revoked_key_ids:
            self.revoked_key_ids.append(key_id)

    def verify_device(self, node_id: str, *, expected_hostname: str) -> TailnetDevice:
        raise AssertionError((node_id, expected_hostname))

    def find_devices(self, *, hostname: str) -> tuple[TailnetDevice, ...]:
        self.find_hostnames.append(hostname)
        return tuple(device for device in self.devices.values() if device.hostname == hostname)

    def remove_device(self, device_id: str) -> None:
        if device_id not in self.removed_device_ids:
            self.removed_device_ids.append(device_id)
        self.devices.pop(device_id, None)


def test_tombstone_survives_ownership_deletion_and_scheduler_removes_late_device(
    isolated_services: ApiServices,
) -> None:
    control_plane = ControlPlaneService(isolated_services.context)
    control_plane.upsert_workspace("default")
    _raw_token, audit_actor = administrator_credential(isolated_services, "workspace-delete-admin")
    workspace = control_plane.upsert_workspace("tailnet-tombstone-owner")
    pool = "tailnet-tombstone-pool"
    isolated_services.compute.create_unit(UnitName(pool), provider="agent", workspace=workspace.id)
    machine = isolated_services.compute.create_machine(
        workspace=workspace.id,
        pool=MachinePool(pool),
    )
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    expires_at = now + timedelta(minutes=5)
    hostname = tailnet_machine_hostname(machine.id, 1)
    control = _TailnetControl()
    store = DatabaseTailnetCleanupStore(isolated_services.context)
    coordinator = TailnetCleanupCoordinator(store, control, settle_seconds=30)
    assert store.pending_count() == 0

    initial = coordinator.defer_machine_cleanup(
        workspace_id=workspace.id,
        pool=MachinePool(pool),
        machine_id=machine.id,
        generations=(1,),
        auth_key_ids=("one-off-key",),
        device_ids=(),
        auth_key_expires_at=expires_at,
        now=now,
    )
    tombstone = store.get_by_machine(machine.id)
    assert initial is not None and initial.rescheduled
    assert tombstone is not None
    assert store.pending_count() == 1

    isolated_services.compute.delete_machine(machine.id, workspace=workspace.id)
    isolated_services.compute.delete_unit(pool, workspace=workspace.id)
    gateway = replace(
        isolated_services.gateway_service,
        compute_state=RedisComputeStateRepository(
            RedisClient(FakeRedis(), key_prefix="tailnet-tombstone-owner")
        ),
    )
    WorkspaceDeletionService(isolated_services, gateway).delete(
        workspace.id,
        audit_actor=audit_actor,
    )

    with isolated_services.context.database.session() as session:
        deleted_workspace = WorkspaceRepository(session).get(workspace.id)
    assert deleted_workspace is not None
    assert deleted_workspace.status is WorkspaceStatus.Deleted
    assert store.get_by_machine(machine.id) is not None
    control.devices["late-device"] = TailnetDevice(
        id="late-device",
        node_id="late-node",
        hostname=hostname,
        authorized=True,
    )

    maintenance = SchedulerMaintenanceControls(tailnet_cleanup=coordinator)
    before = Scheduler(maintenance=maintenance).run_once(
        now=tombstone.not_before - timedelta(seconds=1),
        include_cron_jobs=False,
        include_containers=False,
    )
    after = Scheduler(maintenance=maintenance).run_once(
        now=tombstone.not_before,
        include_cron_jobs=False,
        include_containers=False,
    )

    assert before.tailnet_cleanup_processed_count == 0
    assert after.tailnet_cleanup_processed_count == 1
    assert after.tailnet_cleanup_completed_count == 1
    assert control.removed_device_ids == ["late-device"]
    assert store.get_by_machine(machine.id) is None
    assert store.pending_count() == 0


def test_new_schedule_revision_supersedes_in_flight_cleanup_claim(
    isolated_services: ApiServices,
) -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    store = DatabaseTailnetCleanupStore(isolated_services.context)
    assert store.pending_count() == 0
    first = store.schedule(
        workspace_id="workspace-deleted",
        pool=MachinePool("pool-deleted"),
        machine_id="machine-revision",
        generations=[1],
        auth_key_ids=["key-1"],
        device_ids=[],
        not_before=now + timedelta(minutes=5),
        now=now,
    )
    claimed = store.claim_machine(
        first.machine_id,
        now=now,
        lease_until=now + timedelta(minutes=1),
    )
    assert claimed is not None

    second = store.schedule(
        workspace_id="workspace-deleted",
        pool=MachinePool("pool-deleted"),
        machine_id=first.machine_id,
        generations=[2],
        auth_key_ids=["key-2"],
        device_ids=["device-2"],
        not_before=now + timedelta(minutes=6),
        now=now + timedelta(seconds=1),
    )

    assert second.revision == first.revision + 1
    assert second.generations == [1, 2]
    assert second.auth_key_ids == ["key-1", "key-2"]
    assert second.device_ids == ["device-2"]
    assert not store.complete(claimed)
    retained = store.get_by_machine(first.machine_id)
    assert retained is not None
    assert retained.revision == second.revision
    assert store.pending_count() == 1


def test_scheduler_reports_pending_cleanup_when_control_credentials_are_unavailable(
    isolated_services: ApiServices,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    store = DatabaseTailnetCleanupStore(isolated_services.context)
    unavailable_cleanup = UnavailableTailnetCleanupService(store)

    maintenance = SchedulerMaintenanceControls(tailnet_cleanup=unavailable_cleanup)
    empty = Scheduler(maintenance=maintenance).run_once(
        now=now,
        include_cron_jobs=False,
        include_containers=False,
    )
    store.schedule(
        workspace_id="workspace-deleted",
        pool=MachinePool("pool-deleted"),
        machine_id="machine-pending-control",
        generations=[1],
        auth_key_ids=["key-pending-control"],
        device_ids=[],
        not_before=now,
        now=now,
    )
    with caplog.at_level(logging.WARNING, logger="scheduler.service"):
        blocked = Scheduler(maintenance=maintenance).run_once(
            now=now,
            include_cron_jobs=False,
            include_containers=False,
        )

    assert empty.tailnet_cleanup_failure_count == 0
    assert blocked.tailnet_cleanup_processed_count == 0
    assert blocked.tailnet_cleanup_completed_count == 0
    assert blocked.tailnet_cleanup_failure_count == 1
    assert "tailnet cleanup remains incomplete" in caplog.text


def test_postgresql_concurrent_first_schedules_merge_and_supersede_claim() -> None:
    database_url = os.environ.get("LAZYCLOUD_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("LAZYCLOUD_TEST_POSTGRES_URL is required for PostgreSQL concurrency proof")
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            pool_size=4,
            max_overflow=0,
            application_name=DatabaseApplicationName.Test,
        )
    )
    database.create_schema()
    machine_id = f"tailnet-cleanup-race-{uuid4()}"
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    barrier = Barrier(2)

    def schedule(
        generation: int,
        auth_key_id: str,
        device_id: str,
        not_before: datetime,
    ) -> None:
        barrier.wait(timeout=10)
        with database.session() as session:
            TailnetCleanupTombstoneRepository(session).schedule(
                workspace_id="deleted-workspace",
                pool=MachinePool("deleted-pool"),
                machine_id=machine_id,
                generations=[generation],
                auth_key_ids=[auth_key_id],
                device_ids=[device_id],
                not_before=not_before,
                now=now + timedelta(seconds=generation),
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(
                    schedule,
                    1,
                    "key-1",
                    "device-1",
                    now + timedelta(minutes=5),
                ),
                executor.submit(
                    schedule,
                    2,
                    "key-2",
                    "device-2",
                    now + timedelta(minutes=6),
                ),
            )
            for future in futures:
                future.result(timeout=15)

        with database.session() as session:
            repository = TailnetCleanupTombstoneRepository(session)
            merged = repository.get_by_machine(machine_id)
            assert merged is not None
            assert set(merged.generations) == {1, 2}
            assert set(merged.auth_key_ids) == {"key-1", "key-2"}
            assert set(merged.device_ids) == {"device-1", "device-2"}
            assert merged.not_before == now + timedelta(minutes=6)
            assert merged.revision == 2
            claimed = repository.claim_machine(
                machine_id,
                now=now + timedelta(seconds=3),
                lease_until=now + timedelta(minutes=1),
            )
            assert claimed is not None

        with database.session() as session:
            superseding = TailnetCleanupTombstoneRepository(session).schedule(
                workspace_id="deleted-workspace",
                pool=MachinePool("deleted-pool"),
                machine_id=machine_id,
                generations=[3],
                auth_key_ids=["key-3"],
                device_ids=["device-3"],
                not_before=now + timedelta(minutes=7),
                now=now + timedelta(seconds=4),
            )
            assert superseding.revision == 3
            assert superseding.claim_token == ""
            assert superseding.claimed_until is None

        with database.session() as session:
            repository = TailnetCleanupTombstoneRepository(session)
            assert not repository.complete(claimed)
            retained = repository.get_by_machine(machine_id)
            assert retained is not None
            assert set(retained.generations) == {1, 2, 3}
            assert set(retained.auth_key_ids) == {"key-1", "key-2", "key-3"}
            assert set(retained.device_ids) == {"device-1", "device-2", "device-3"}
    finally:
        with database.session() as session:
            session.execute(
                delete(TailnetCleanupTombstoneTable).where(
                    TailnetCleanupTombstoneTable.machine_id == machine_id
                )
            )
        database.dispose()
