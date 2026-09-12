from __future__ import annotations

import pytest
from api.server.services import ApiServices
from execution.volumes.control import VolumeControlService
from shared.errors import InvalidInputError
from shared.http.volumes import (
    DeleteVolumeRequest,
    GetOrCreateVolumeRequest,
    ListPathRequest,
    MovePathRequest,
    StatPathRequest,
)
from storage.volume_filesystem import (
    VolumeNamespace,
    WorkspaceVolumeFilesystem,
    WorkspaceVolumeStore,
)
from tests.fakes import FakeObjectClient
from tests.workspaces import owned_workspace


def test_volume_control_isolates_same_name_by_stable_workspace_and_volume_ids(
    isolated_services: ApiServices,
) -> None:
    filesystem = isolated_services.volume_filesystem
    assert isinstance(filesystem, WorkspaceVolumeFilesystem)
    client = isolated_services.object_storage.object_client
    assert isinstance(client, FakeObjectClient)
    service = VolumeControlService(isolated_services, filesystem=filesystem)
    control = isolated_services.control_plane_service
    default_workspace = control.get_workspace()
    other_workspace = owned_workspace(control, "other")
    control.ensure_workspace_storage(other_workspace.id)

    default = service.get_or_create_volume(GetOrCreateVolumeRequest(name="data"))
    other = service.get_or_create_volume(
        GetOrCreateVolumeRequest(name="data"),
        workspace_id=other_workspace.id,
    )
    assert default.volume is not None
    assert other.volume is not None
    assert default.volume.id != other.volume.id

    service.copy_path("data/nested/payload.txt", b"default", workspace_id=default_workspace.id)
    service.copy_path("data/nested/payload.txt", b"other", workspace_id=other_workspace.id)

    default_bucket = f"{client.settings.workspace_bucket_prefix}-{default_workspace.id}"
    other_bucket = f"{client.settings.workspace_bucket_prefix}-{other_workspace.id}"
    default_key = f"volumes/{default.volume.id}/nested/payload.txt"
    other_key = f"volumes/{other.volume.id}/nested/payload.txt"
    assert client.read_bytes(default_key, bucket=default_bucket) == b"default"
    assert client.read_bytes(other_key, bucket=other_bucket) == b"other"
    other_stat = service.stat_path(
        StatPathRequest(path="data/nested/payload.txt"),
        workspace_id=other_workspace.id,
    ).path_info
    assert other_stat is not None
    assert other_stat.size == 5
    assert [
        item.path
        for item in service.list_path(
            ListPathRequest(path="data/nested"),
            workspace_id=default_workspace.id,
        ).path_infos
    ] == ["nested/payload.txt"]

    service.move_path(
        MovePathRequest(
            original_path="data/nested/payload.txt",
            new_path="data/archive/payload.txt",
        ),
        workspace_id=default_workspace.id,
    )
    service.delete_volume(
        DeleteVolumeRequest(name="data"),
        workspace_id=default_workspace.id,
    )

    assert not client.list_prefix(f"volumes/{default.volume.id}/", bucket=default_bucket)
    assert client.read_bytes(other_key, bucket=other_bucket) == b"other"
    assert [item.id for item in service.list_volumes(workspace_id=other_workspace.id).volumes] == [
        other.volume.id
    ]


def test_workspace_volumes_sharing_a_volume_id_stay_in_their_own_buckets() -> None:
    # The key carries no workspace segment, so the bucket resolved per workspace
    # is the only thing keeping two tenants apart.
    client = FakeObjectClient()
    filesystem = _workspace_filesystem(client)
    first = VolumeNamespace("workspace-a", "volume-id")
    second = VolumeNamespace("workspace-b", "volume-id")

    filesystem.write_path(first, "nested/payload.txt", (b"pay", b"load"))
    filesystem.write_path(second, "nested/payload.txt", (b"other",))
    filesystem.move_path(first, "nested/payload.txt", "archive/payload.txt")

    assert client.objects[("workspace-workspace-a", "volumes/volume-id/archive/payload.txt")] == (
        b"payload"
    )
    assert ("workspace-workspace-a", "volumes/volume-id/nested/payload.txt") not in client.objects
    assert client.objects[("workspace-workspace-b", "volumes/volume-id/nested/payload.txt")] == (
        b"other"
    )
    assert filesystem.occupancy_bytes(first) == 7

    filesystem.delete_volume(first)

    assert not any(bucket == "workspace-workspace-a" for bucket, _ in client.objects)
    assert ("workspace-workspace-b", "volumes/volume-id/nested/payload.txt") in client.objects


def test_move_path_rejects_an_occupied_destination_without_touching_either_side() -> None:
    client = FakeObjectClient()
    filesystem = _workspace_filesystem(client)
    namespace = VolumeNamespace("workspace", "volume")
    filesystem.write_path(namespace, "source.txt", (b"source",))
    filesystem.write_path(namespace, "target.txt", (b"target",))

    with pytest.raises(InvalidInputError):
        filesystem.move_path(namespace, "source.txt", "target.txt")

    assert client.objects[("workspace-workspace", "volumes/volume/source.txt")] == b"source"
    assert client.objects[("workspace-workspace", "volumes/volume/target.txt")] == b"target"


def test_delete_path_does_not_delete_sibling_prefixes() -> None:
    client = FakeObjectClient()
    filesystem = _workspace_filesystem(client)
    namespace = VolumeNamespace("workspace", "volume")
    filesystem.write_path(namespace, "directory/file.txt", (b"delete",))
    filesystem.write_path(namespace, "directory-sibling/file.txt", (b"keep",))

    deleted = filesystem.delete_path(namespace, "directory")

    assert deleted == ("directory/file.txt",)
    assert ("workspace-workspace", "volumes/volume/directory/file.txt") not in client.objects
    assert (
        client.objects[("workspace-workspace", "volumes/volume/directory-sibling/file.txt")]
        == b"keep"
    )


def _workspace_filesystem(client: FakeObjectClient) -> WorkspaceVolumeFilesystem:
    # Production gives every workspace its own bucket; the fake resolver keeps
    # that shape so key collisions across tenants would surface here.
    return WorkspaceVolumeFilesystem(
        resolve_store=lambda workspace_id: WorkspaceVolumeStore(
            client=client,
            bucket=f"workspace-{workspace_id}",
        )
    )
