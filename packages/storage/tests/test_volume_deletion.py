from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from api.server.services import ApiServices
from database.repositories.storage import VolumeRepository
from psycopg.errors import LockNotAvailable
from shared.errors import ConflictError
from shared.http.volumes import DeleteVolumeRequest, GetOrCreateVolumeRequest
from shared.timestamps import utc_now
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from storage.volume_filesystem import VolumeFilesystem, VolumeNamespace


def test_mount_admissions_share_volume_fence_and_block_deletion(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    control = services.volume_service
    volume = control.get_or_create_volume(GetOrCreateVolumeRequest(name="mount-fence")).volume
    assert volume is not None

    def reserve_mount() -> None:
        with services.database.session() as session:
            VolumeRepository(session).lock_mounts({volume.name}, workspace_id=volume.workspace_id)

    with ThreadPoolExecutor(max_workers=1) as executor, services.database.session() as session:
        VolumeRepository(session).lock_mounts({volume.name}, workspace_id=volume.workspace_id)
        executor.submit(reserve_mount).result(timeout=3)
        with (
            pytest.raises(OperationalError) as rejected,
            services.database.session() as deleting,
        ):
            deleting.execute(text("SET LOCAL lock_timeout = '100ms'"))
            control.deletion.request_in_session(
                deleting, volume.name, workspace_id=volume.workspace_id
            )
        assert isinstance(rejected.value.orig, LockNotAvailable)
    with services.database.session() as session:
        control.deletion.request_in_session(session, volume.name, workspace_id=volume.workspace_id)
    with pytest.raises(ConflictError, match="deleting"):
        reserve_mount()


def test_failed_deletion_stops_billing_and_retries_without_losing_ownership(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = isolated_services.volume_service
    created = control.get_or_create_volume(GetOrCreateVolumeRequest(name="retiring"))
    assert created.volume is not None
    volume = created.volume
    namespace = VolumeNamespace(volume.workspace_id, volume.id)
    control.copy_path("retiring/data", b"data")
    with isolated_services.database.session() as session:
        row = VolumeRepository(session).lock(volume.name, workspace_id=volume.workspace_id)
        row.unfenced_writes_possible = True
        row.size_bytes = 4
        row.metered_at = utc_now() - timedelta(minutes=2)

    def unavailable(_filesystem: VolumeFilesystem, _namespace: VolumeNamespace) -> None:
        raise ConnectionError("storage is unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(type(control.filesystem), "delete_volume", unavailable)
        assert not control.delete_volume(DeleteVolumeRequest(name=volume.name)).deleted
    pending = control.list_volumes().volumes[0]
    assert pending.id == volume.id and pending.deletion_requested_at is not None
    with pytest.raises(ConflictError, match="deleting"):
        control.copy_path("retiring/rejected", b"rejected")
    assert (
        isolated_services.volume_metering.reconcile_volume(
            volume.name, workspace_id=volume.workspace_id, now=utc_now() + timedelta(hours=1)
        )
        is None
    )
    control.deletion.reconcile_due()
    assert not control.list_volumes().volumes

    replacement = control.get_or_create_volume(GetOrCreateVolumeRequest(name=volume.name)).volume
    assert replacement is not None and replacement.id != volume.id
    control.copy_path("retiring/keep", b"keep")
    control.filesystem.write_path(namespace, "late-put", (b"late",))
    control.deletion.reconcile_due(now=utc_now() + timedelta(hours=2))
    assert control.filesystem.occupancy_bytes(namespace) == 0
    assert (
        control.filesystem.occupancy_bytes(VolumeNamespace(volume.workspace_id, replacement.id))
        == 4
    )


def test_slow_storage_cleanup_does_not_lock_volume_or_delete_its_replacement(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = isolated_services.volume_service
    volume = control.get_or_create_volume(GetOrCreateVolumeRequest(name="overlap")).volume
    assert volume is not None
    control.copy_path("overlap/data", b"old")
    with isolated_services.database.session() as session:
        control.deletion.request_in_session(session, volume.name, workspace_id=volume.workspace_id)
    entered, release = Event(), Event()
    original = type(control.filesystem).delete_volume

    def stalled(filesystem: VolumeFilesystem, namespace: VolumeNamespace) -> None:
        entered.set()
        assert release.wait(5)
        original(filesystem, namespace)

    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            with monkeypatch.context() as patch:
                patch.setattr(type(control.filesystem), "delete_volume", stalled)
                first = executor.submit(
                    control.deletion.finish, volume.name, workspace_id=volume.workspace_id
                )
                assert entered.wait(5)
            assert control.deletion.finish(volume.name, workspace_id=volume.workspace_id)
            replacement = control.get_or_create_volume(
                GetOrCreateVolumeRequest(name=volume.name)
            ).volume
            assert replacement is not None and replacement.id != volume.id
            control.copy_path("overlap/keep", b"new")
        finally:
            release.set()
        assert first.result(timeout=5)
    assert (
        control.filesystem.occupancy_bytes(VolumeNamespace(volume.workspace_id, replacement.id))
        == 3
    )
