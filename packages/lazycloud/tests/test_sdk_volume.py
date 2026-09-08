from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import pytest
from lazycloud.abstractions.volume import (
    CloudBucketConfig,
    Volume,
    volume_mounts,
)
from shared.deployment_records import VolumeMount
from shared.errors import InvalidInputError, NotFoundError
from shared.http import volumes
from shared.http.volumes import (
    AbortMultipartUploadRequest,
    AbortMultipartUploadResponse,
    CompleteMultipartUploadRequest,
    CompleteMultipartUploadResponse,
    CopyPathResponse,
    CreateMultipartUploadRequest,
    CreateMultipartUploadResponse,
    CreatePresignedUrlResponse,
    DeletePathRequest,
    DeletePathResponse,
    DeleteVolumeResponse,
    GetFileServiceInfoResponse,
    GetOrCreateVolumeResponse,
    ListPathRequest,
    ListPathResponse,
    MovePathRequest,
    MovePathResponse,
    PathInfo,
    PresignedUrlMethod,
    StatPathRequest,
    StatPathResponse,
    VolumeInstance,
)
from shared.timestamps import utc_now

ReturnT = TypeVar("ReturnT")


def _call_runtime(
    function: Callable[..., ReturnT],
    /,
    *args: object,
    **kwargs: object,
) -> ReturnT:
    return function(*args, **kwargs)


@dataclass
class FakeVolumeClient:
    objects: dict[str, bytes] = field(default_factory=dict)
    volumes: dict[str, VolumeInstance] = field(default_factory=dict)
    urls: dict[tuple[str, str, PresignedUrlMethod], str] = field(default_factory=dict)
    deleted_volume: str = ""
    fail_next: str = ""

    def create(self, name: str) -> GetOrCreateVolumeResponse:
        if self.fail_next == "create":
            raise InvalidInputError("create failed")
        volume = self.volumes.setdefault(name, _volume(name))
        return GetOrCreateVolumeResponse(volume=volume)

    def delete(self, name: str) -> DeleteVolumeResponse:
        if self.fail_next == "delete":
            raise InvalidInputError("delete failed")
        self.deleted_volume = name
        self.volumes.pop(name, None)
        return DeleteVolumeResponse()

    def copy(self, path: str, content: bytes) -> CopyPathResponse:
        if self.fail_next == "copy":
            raise InvalidInputError("copy failed")
        self.objects[path] = content
        return CopyPathResponse(object_id=f"obj-{path}")

    def list_path(self, request: ListPathRequest) -> ListPathResponse:
        prefix = request.path.rstrip("/")
        infos = [
            _path_info(path.removeprefix(f"{prefix}/"), content)
            for path, content in sorted(self.objects.items())
            if path.startswith(f"{prefix}/")
        ]
        return ListPathResponse(path_infos=tuple(infos))

    def stat_path(self, request: StatPathRequest) -> StatPathResponse:
        if request.path not in self.objects:
            raise NotFoundError("missing")
        return StatPathResponse(
            path_info=_path_info(request.path, self.objects[request.path]),
        )

    def move_path(self, request: MovePathRequest) -> MovePathResponse:
        if request.original_path not in self.objects:
            raise NotFoundError("missing")
        self.objects[request.new_path] = self.objects.pop(request.original_path)
        return MovePathResponse(new_path=request.new_path)

    def delete_path(self, request: DeletePathRequest) -> DeletePathResponse:
        if request.path not in self.objects:
            raise NotFoundError("missing")
        del self.objects[request.path]
        return DeletePathResponse(deleted=(request.path,))

    def get_file_service_info(self) -> GetFileServiceInfoResponse:
        return GetFileServiceInfoResponse(enabled=True, command_version=2)

    def presigned_url(
        self,
        volume_name: str,
        volume_path: str,
        *,
        method: PresignedUrlMethod = PresignedUrlMethod.GetObject,
        expires: int = 0,
        upload_id: str = "",
        part_number: int = 0,
    ) -> CreatePresignedUrlResponse:
        _ = (expires, upload_id, part_number)
        return CreatePresignedUrlResponse(
            url=self.urls.get(
                (volume_name, volume_path, method),
                f"https://objects.example/{volume_name}/{volume_path}?method={method.value}",
            ),
        )

    def create_multipart_upload(
        self,
        request: CreateMultipartUploadRequest,
    ) -> CreateMultipartUploadResponse:
        return CreateMultipartUploadResponse(
            upload_id="upload_1",
            file_upload_parts=(
                volumes.FileUploadPart(
                    number=1, start=0, end=request.file_size, url="https://part-1"
                ),
            ),
        )

    def complete_multipart_upload(
        self,
        request: CompleteMultipartUploadRequest,
    ) -> CompleteMultipartUploadResponse:
        assert request.completed_parts[0].etag == "etag-1"
        return CompleteMultipartUploadResponse()

    def abort_multipart_upload(
        self,
        request: AbortMultipartUploadRequest,
    ) -> AbortMultipartUploadResponse:
        assert request.upload_id == "upload_2"
        return AbortMultipartUploadResponse()


@dataclass(frozen=True)
class ExportableVolume:
    name: str
    mount_path: str

    def export(self) -> VolumeMount:
        return VolumeMount(name=self.name, mount_path=self.mount_path)


def test_volume_control_client_backed_file_operations(tmp_path: Path) -> None:
    client = FakeVolumeClient()
    volume = Volume("data", mount_path="./mounted-data")._bind_control(client)

    assert volume.get_or_create() is True
    assert volume.volume_id == "vol_data"
    assert volume.export().mount_path == "mounted-data"
    assert volume.write_text("notes/a.txt", "hello") == "data/notes/a.txt"
    assert client.objects["data/notes/a.txt"] == b"hello"
    assert volume.list("notes") == ["a.txt"]
    assert volume.stat("notes/a.txt").size == 5
    assert volume.file_service_info().command_version == 2

    moved = volume.move("notes/a.txt", "archive/a.txt")
    assert moved == "data/archive/a.txt"
    assert volume.remove("archive/a.txt") == ("data/archive/a.txt",)

    source = tmp_path / "upload.txt"
    source.write_text("payload", encoding="utf-8")
    assert volume.put(source, "uploads/upload.txt") == "data/uploads/upload.txt"
    assert client.objects["data/uploads/upload.txt"] == b"payload"


def test_volume_mounts_accept_exportables_and_reject_invalid_items() -> None:
    explicit = VolumeMount(name="models", mount_path="/models")
    exported = ExportableVolume(name="data", mount_path="/data")

    assert volume_mounts([explicit, exported]) == [
        explicit,
        VolumeMount(name="data", mount_path="/data"),
    ]

    with pytest.raises(TypeError, match="unsupported volume type: object"):
        _call_runtime(volume_mounts, [object()])


def test_volume_rejects_unsafe_paths_and_raises_typed_errors() -> None:
    client = FakeVolumeClient(fail_next="copy")
    volume = Volume("data")._bind_control(client)

    with pytest.raises(ValueError, match="unsafe"):
        volume.write_text("../escape.txt", "nope")
    with pytest.raises(InvalidInputError, match="copy failed"):
        volume.write_text("safe.txt", "nope")


def test_volume_delete_updates_ready_state() -> None:
    client = FakeVolumeClient()
    volume = Volume("data")._bind_control(client)
    volume.create()

    assert volume.ready is True
    assert volume.delete() is True
    assert volume.ready is False
    assert volume.volume_id is None
    assert client.deleted_volume == "data"


def test_cloud_bucket_config_rejects_partial_secret_references() -> None:
    with pytest.raises(ValueError, match="both be set or both be omitted"):
        CloudBucketConfig(access_key="ACCESS_SECRET")


def test_cloud_bucket_config_rejects_parent_prefix_segments() -> None:
    with pytest.raises(ValueError, match=r"cannot contain '\.\.'"):
        CloudBucketConfig(prefix="models/../private")


def _volume(name: str) -> VolumeInstance:
    timestamp = utc_now()
    return VolumeInstance(
        id=f"vol_{name}",
        name=name,
        size=0,
        created_at=timestamp,
        updated_at=timestamp,
        workspace_id="2f7c8516-6170-4b62-8252-6ef8b38a34af",
        workspace_name="default",
    )


def _path_info(path: str, content: bytes) -> PathInfo:
    return PathInfo(
        path=path,
        size=len(content),
        mod_time=utc_now(),
        is_dir=False,
    )
