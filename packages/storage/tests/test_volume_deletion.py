from datetime import timedelta

import pytest
from api.server.services import ApiServices
from database.repositories.storage import VolumeRepository
from shared.errors import ConflictError
from shared.http.volumes import DeleteVolumeRequest, GetOrCreateVolumeRequest
from shared.timestamps import utc_now
from storage.volume_filesystem import VolumeFilesystem, VolumeNamespace


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
