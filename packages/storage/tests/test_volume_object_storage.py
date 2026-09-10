from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from execution.volumes.control import VolumeControlService
from shared.errors import InvalidInputError
from shared.http.volumes import (
    DeleteVolumeRequest,
    GetOrCreateVolumeRequest,
    ListPathRequest,
    MovePathRequest,
    PresignedUrlMethod,
    StatPathRequest,
)
from storage.volume_filesystem import (
    LocalVolumeFilesystem,
    VolumeNamespace,
    WorkspaceVolumeFilesystem,
    WorkspaceVolumeStore,
)
from storage_client.s3 import S3ObjectInfo
from tests.workspaces import owned_workspace


def test_volume_control_isolates_same_name_by_stable_workspace_and_volume_ids(
    isolated_services: ApiServices,
) -> None:
    filesystem = isolated_services.volume_filesystem
    assert isinstance(filesystem, LocalVolumeFilesystem)
    service = VolumeControlService(isolated_services, filesystem=filesystem)
    control = ControlPlaneService(isolated_services.context)
    default_workspace = control.get_workspace()
    other_workspace = owned_workspace(control, "other")

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

    default_namespace = VolumeNamespace(default_workspace.id, default.volume.id)
    other_namespace = VolumeNamespace(other_workspace.id, other.volume.id)
    assert (
        filesystem.resolve_path(default_namespace, "nested/payload.txt").read_bytes() == b"default"
    )
    assert filesystem.resolve_path(other_namespace, "nested/payload.txt").read_bytes() == b"other"
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

    assert not filesystem.resolve_path(default_namespace).exists()
    assert filesystem.resolve_path(other_namespace, "nested/payload.txt").read_bytes() == b"other"
    assert [item.id for item in service.list_volumes(workspace_id=other_workspace.id).volumes] == [
        other.volume.id
    ]


def test_workspace_volumes_sharing_a_volume_id_stay_in_their_own_buckets() -> None:
    # The key carries no workspace segment, so the bucket resolved per workspace
    # is the only thing keeping two tenants apart.
    client = _FakeObjectClient()
    filesystem = _workspace_filesystem(client)
    first = VolumeNamespace("workspace-a", "volume-id")
    second = VolumeNamespace("workspace-b", "volume-id")

    filesystem.write_path(first, "nested/payload.txt", (b"pay", b"load"))
    filesystem.write_path(second, "nested/payload.txt", (b"other",))
    filesystem.move_path(first, "nested/payload.txt", "archive/payload.txt")
    upload_id = filesystem.create_multipart_upload(first, "large.bin")
    upload_url = filesystem.create_presigned_url(
        first,
        "large.bin",
        method=PresignedUrlMethod.UploadPart,
        expires_seconds=60,
        upload_id=upload_id,
        part_number=1,
    )
    filesystem.complete_multipart_upload(
        first,
        "large.bin",
        upload_id=upload_id,
        completed_parts=((1, "etag-1"),),
    )

    assert client.objects[
        ("workspace-workspace-a", "volumes/volume-id/archive/payload.txt")
    ].data == (b"payload")
    assert ("workspace-workspace-a", "volumes/volume-id/nested/payload.txt") not in client.objects
    assert client.objects[
        ("workspace-workspace-b", "volumes/volume-id/nested/payload.txt")
    ].data == (b"other")
    assert filesystem.occupancy_bytes(first) == 7
    assert upload_url == (
        "https://storage.invalid/workspace-workspace-a/volumes/volume-id/large.bin"
        "?upload=upload-1&part=1&expires=60"
    )
    assert client.completed == [
        (
            "workspace-workspace-a",
            "volumes/volume-id/large.bin",
            "upload-1",
            ((1, "etag-1"),),
        )
    ]

    filesystem.delete_volume(first)

    assert client.deleted_prefixes == ["volumes/volume-id/"]
    assert not any(bucket == "workspace-workspace-a" for bucket, _ in client.objects)
    assert ("workspace-workspace-b", "volumes/volume-id/nested/payload.txt") in client.objects


def test_move_path_rejects_an_occupied_destination_without_touching_either_side() -> None:
    client = _FakeObjectClient()
    filesystem = _workspace_filesystem(client)
    namespace = VolumeNamespace("workspace", "volume")
    filesystem.write_path(namespace, "source.txt", (b"source",))
    filesystem.write_path(namespace, "target.txt", (b"target",))

    with pytest.raises(InvalidInputError):
        filesystem.move_path(namespace, "source.txt", "target.txt")

    assert client.objects[("workspace-workspace", "volumes/volume/source.txt")].data == b"source"
    assert client.objects[("workspace-workspace", "volumes/volume/target.txt")].data == b"target"


def test_delete_path_does_not_delete_sibling_prefixes() -> None:
    client = _FakeObjectClient()
    filesystem = _workspace_filesystem(client)
    namespace = VolumeNamespace("workspace", "volume")
    filesystem.write_path(namespace, "directory/file.txt", (b"delete",))
    filesystem.write_path(namespace, "directory-sibling/file.txt", (b"keep",))

    deleted = filesystem.delete_path(namespace, "directory")

    assert deleted == ("directory/file.txt",)
    assert ("workspace-workspace", "volumes/volume/directory/file.txt") not in client.objects
    assert (
        client.objects[("workspace-workspace", "volumes/volume/directory-sibling/file.txt")].data
        == b"keep"
    )


@dataclass(frozen=True, slots=True)
class _FakeObject:
    data: bytes
    modified_at: datetime


class _FakeObjectClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], _FakeObject] = {}
        self.completed: list[tuple[str, str, str, tuple[tuple[int, str], ...]]] = []
        self.aborted: list[tuple[str, str, str]] = []
        self.uploads: dict[tuple[str, str, str], None] = {}
        self.deleted_prefixes: list[str] = []
        self._upload_number = 0

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        del content_type, metadata
        target_bucket = bucket or "default"
        payload = Path(source).read_bytes()
        self.objects[(target_bucket, key)] = _FakeObject(payload, datetime.now(UTC))
        return self.head(key, bucket=target_bucket)

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        return (bucket or "default", key) in self.objects

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        target_bucket = bucket or "default"
        item = self.objects[(target_bucket, key)]
        return S3ObjectInfo(
            bucket=target_bucket,
            key=key,
            size=len(item.data),
            last_modified=item.modified_at,
        )

    def list_directory(
        self,
        prefix: str,
        *,
        bucket: str | None = None,
    ) -> tuple[S3ObjectInfo, ...]:
        target_bucket = bucket or "default"
        directory = f"{prefix.rstrip('/')}/"
        entries: dict[str, S3ObjectInfo] = {}
        for (item_bucket, key), item in self.objects.items():
            if item_bucket != target_bucket or not key.startswith(directory):
                continue
            remainder = key.removeprefix(directory)
            first, separator, _ = remainder.partition("/")
            entry_key = f"{directory}{first}/" if separator else key
            entries.setdefault(
                entry_key,
                S3ObjectInfo(
                    bucket=target_bucket,
                    key=entry_key,
                    size=0 if separator else len(item.data),
                    last_modified=item.modified_at,
                ),
            )
        return tuple(entries[key] for key in sorted(entries))

    def list_prefix(
        self,
        prefix: str,
        *,
        bucket: str | None = None,
    ) -> tuple[S3ObjectInfo, ...]:
        target_bucket = bucket or "default"
        return tuple(
            S3ObjectInfo(
                bucket=target_bucket,
                key=key,
                size=len(item.data),
                last_modified=item.modified_at,
            )
            for (item_bucket, key), item in sorted(self.objects.items())
            if item_bucket == target_bucket and key.startswith(prefix)
        )

    def delete_prefix(self, prefix: str, *, bucket: str | None = None) -> tuple[str, ...]:
        target_bucket = bucket or "default"
        self.deleted_prefixes.append(prefix)
        keys = tuple(
            key
            for item_bucket, key in self.objects
            if item_bucket == target_bucket and key.startswith(prefix)
        )
        for key in keys:
            del self.objects[(target_bucket, key)]
        return keys

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        self.objects.pop((bucket or "default", key), None)

    def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        bucket: str | None = None,
        source_bucket: str | None = None,
    ) -> None:
        target_bucket = bucket or "default"
        item = self.objects[(source_bucket or target_bucket, source_key)]
        self.objects[(target_bucket, destination_key)] = item

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return self._url(key, bucket=bucket, suffix=f"get&expires={expires_seconds}")

    def generate_presigned_head_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return self._url(key, bucket=bucket, suffix=f"head&expires={expires_seconds}")

    def create_multipart_upload(self, key: str, *, bucket: str | None = None) -> str:
        self._upload_number += 1
        upload_id = f"upload-{self._upload_number}"
        self.uploads[(bucket or "default", key, upload_id)] = None
        return upload_id

    def generate_presigned_upload_part_url(
        self,
        key: str,
        *,
        upload_id: str,
        part_number: int,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return self._url(
            key,
            bucket=bucket,
            suffix=f"upload={upload_id}&part={part_number}&expires={expires_seconds}",
        )

    def complete_multipart_upload(
        self,
        key: str,
        *,
        upload_id: str,
        completed_parts: tuple[tuple[int, str], ...],
        bucket: str | None = None,
    ) -> None:
        self.completed.append((bucket or "default", key, upload_id, completed_parts))
        self.uploads.pop((bucket or "default", key, upload_id), None)

    def abort_multipart_upload(
        self,
        key: str,
        *,
        upload_id: str,
        bucket: str | None = None,
    ) -> None:
        self.aborted.append((bucket or "default", key, upload_id))
        self.uploads.pop((bucket or "default", key, upload_id), None)

    def abort_multipart_uploads(self, prefix: str, *, bucket: str | None = None) -> None:
        for target_bucket, key, upload_id in tuple(self.uploads):
            if target_bucket == (bucket or "default") and key.startswith(prefix):
                self.abort_multipart_upload(key, bucket=target_bucket, upload_id=upload_id)

    @staticmethod
    def _url(key: str, *, bucket: str | None, suffix: str) -> str:
        return f"https://storage.invalid/{bucket or 'default'}/{key}?{suffix}"


def _workspace_filesystem(client: _FakeObjectClient) -> WorkspaceVolumeFilesystem:
    # Production gives every workspace its own bucket; the fake resolver keeps
    # that shape so key collisions across tenants would surface here.
    return WorkspaceVolumeFilesystem(
        resolve_store=lambda workspace_id: WorkspaceVolumeStore(
            client=client,
            bucket=f"workspace-{workspace_id}",
        )
    )
