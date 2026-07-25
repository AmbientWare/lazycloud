from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.observability import UsageRepository
from database.tables.storage import VolumeTable
from execution.volumes.control import VolumeControlService
from shared.http.volumes import (
    CompletedPart,
    CompleteMultipartUploadRequest,
    CreateMultipartUploadRequest,
    CreatePresignedUrlRequest,
    DeleteVolumeRequest,
    GetOrCreateVolumeRequest,
    ListPathRequest,
    MovePathRequest,
    PresignedUrlMethod,
    PresignedUrlParams,
    StatPathRequest,
)
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
)
from sqlalchemy import select
from storage.volume_filesystem import (
    JuiceFsGatewayVolumeFilesystem,
    LocalVolumeFilesystem,
    VolumeNamespace,
)
from storage.volume_metering import PersistentVolumeMeteringService
from storage_client.s3 import S3ObjectInfo


def test_volume_control_isolates_same_name_by_stable_workspace_and_volume_ids(
    isolated_services: ApiServices,
) -> None:
    filesystem = isolated_services.volume_filesystem
    assert isinstance(filesystem, LocalVolumeFilesystem)
    service = VolumeControlService(isolated_services, filesystem=filesystem)
    control = ControlPlaneService(isolated_services.context)
    default_workspace = control.get_workspace()
    other_workspace = control.upsert_workspace("other")

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


def test_juicefs_gateway_uses_one_canonical_namespace_for_files_and_multipart() -> None:
    client = _FakeGatewayClient()
    filesystem = _gateway_filesystem(client)
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
        ("lazycloud", "volumes/workspace-a/volume-id/archive/payload.txt")
    ].data == (b"payload")
    assert client.objects[
        ("lazycloud", "volumes/workspace-b/volume-id/nested/payload.txt")
    ].data == (b"other")
    assert filesystem.occupancy_bytes(first) == 7
    assert upload_url == (
        "https://gateway.invalid/lazycloud/volumes/workspace-a/volume-id/large.bin"
        "?upload=upload-1&part=1&expires=60"
    )
    assert client.completed == [
        (
            "lazycloud",
            "volumes/workspace-a/volume-id/large.bin",
            "upload-1",
            ((1, "etag-1"),),
        )
    ]

    filesystem.delete_volume(first)

    assert client.deleted_prefixes == ["volumes/workspace-a/volume-id/"]
    assert not any(key.startswith("volumes/workspace-a/volume-id") for _, key in client.objects)
    assert ("lazycloud", "volumes/workspace-b/volume-id/nested/payload.txt") in client.objects


def test_volume_control_presigned_and_multipart_requests_use_database_identity(
    isolated_services: ApiServices,
) -> None:
    client = _FakeGatewayClient()
    filesystem = _gateway_filesystem(client)
    service = VolumeControlService(isolated_services, filesystem=filesystem)
    created = service.get_or_create_volume(GetOrCreateVolumeRequest(name="data"))
    assert created.volume is not None

    presigned = service.create_presigned_url(
        CreatePresignedUrlRequest(
            volume_name="data",
            volume_path="payload.txt",
            method=PresignedUrlMethod.PutObject,
            expires=120,
            params=PresignedUrlParams(
                content_length=7,
                content_type="text/plain",
            ),
        )
    )
    multipart = service.create_multipart_upload(
        CreateMultipartUploadRequest(
            volume_name="data",
            volume_path="large.bin",
            file_size=11,
            chunk_size=5,
        )
    )
    service.complete_multipart_upload(
        CompleteMultipartUploadRequest(
            upload_id=multipart.upload_id,
            volume_name="data",
            volume_path="large.bin",
            completed_parts=(CompletedPart(number=1, etag="etag-1"),),
        )
    )

    namespace_key = f"volumes/{created.volume.workspace_id}/{created.volume.id}"
    assert presigned.url == (
        f"https://gateway.invalid/lazycloud/{namespace_key}/payload.txt?put&expires=120"
    )
    assert [(part.number, part.start, part.end) for part in multipart.file_upload_parts] == [
        (1, 0, 5),
        (2, 5, 10),
        (3, 10, 11),
    ]
    assert all(f"/{namespace_key}/large.bin?" in part.url for part in multipart.file_upload_parts)
    assert client.completed == [
        (
            "lazycloud",
            f"{namespace_key}/large.bin",
            multipart.upload_id,
            ((1, "etag-1"),),
        )
    ]


def test_gateway_delete_path_does_not_delete_sibling_prefixes() -> None:
    client = _FakeGatewayClient()
    filesystem = _gateway_filesystem(client)
    namespace = VolumeNamespace("workspace", "volume")
    filesystem.write_path(namespace, "directory/file.txt", (b"delete",))
    filesystem.write_path(namespace, "directory-sibling/file.txt", (b"keep",))

    deleted = filesystem.delete_path(namespace, "directory")

    assert deleted == ("directory/file.txt",)
    assert ("lazycloud", "volumes/workspace/volume/directory/file.txt") not in client.objects
    assert (
        client.objects[("lazycloud", "volumes/workspace/volume/directory-sibling/file.txt")].data
        == b"keep"
    )


def test_storage_delete_failure_keeps_volume_metadata_retriable(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    filesystem = _FailingDeleteFilesystem(isolated_services.context.paths.root / "failing-volumes")
    services = _services_with_volume_metering(
        isolated_services,
        filesystem,
        PersistentVolumeMeteringService(
            isolated_services.context,
            filesystem,
        ),
        request,
    )
    service = services.volume_service
    created = service.get_or_create_volume(GetOrCreateVolumeRequest(name="retry"))
    assert created.volume is not None
    service.copy_path("retry/payload.txt", b"payload")
    _set_volume_checkpoint(services, "retry")

    with pytest.raises(OSError, match="storage delete unavailable"):
        service.delete_volume(DeleteVolumeRequest(name="retry"))

    assert [item.name for item in service.list_volumes().volumes] == ["retry"]
    assert filesystem.resolve_path(
        VolumeNamespace(created.volume.workspace_id, created.volume.id),
        "payload.txt",
    ).is_file()

    filesystem.fail_delete = False
    service.delete_volume(DeleteVolumeRequest(name="retry"))

    assert service.list_volumes().volumes == ()


def _services_with_volume_metering(
    isolated_services: ApiServices,
    filesystem: LocalVolumeFilesystem,
    volume_metering: PersistentVolumeMeteringService,
    request: pytest.FixtureRequest,
) -> ApiServices:
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        volume_metering=volume_metering,
        volume_filesystem=filesystem,
        redis_client=isolated_services.redis_client,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services
    with isolated_services.context.database.session() as session:
        records = [
            record
            for record in UsageRepository(session).list_across_workspaces()
            if record.metric is UsageMetric.PersistentVolumeByteSeconds
            and record.resource_id == "retry"
        ]
    records.sort(key=lambda record: record.created_at)
    assert len(records) == 2
    assert (
        records[0].metadata[METERING_WINDOW_ENDED_AT_METADATA_KEY]
        == records[1].metadata[METERING_WINDOW_STARTED_AT_METADATA_KEY]
    )


@dataclass(frozen=True, slots=True)
class _FakeObject:
    data: bytes
    modified_at: datetime


class _FakeGatewayClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], _FakeObject] = {}
        self.completed: list[tuple[str, str, str, tuple[tuple[int, str], ...]]] = []
        self.aborted: list[tuple[str, str, str]] = []
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

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str:
        del content_length, content_type
        return self._url(key, bucket=bucket, suffix=f"put&expires={expires_seconds}")

    def create_multipart_upload(self, key: str, *, bucket: str | None = None) -> str:
        del key, bucket
        self._upload_number += 1
        return f"upload-{self._upload_number}"

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

    def abort_multipart_upload(
        self,
        key: str,
        *,
        upload_id: str,
        bucket: str | None = None,
    ) -> None:
        self.aborted.append((bucket or "default", key, upload_id))

    @staticmethod
    def _url(key: str, *, bucket: str | None, suffix: str) -> str:
        return f"https://gateway.invalid/{bucket or 'default'}/{key}?{suffix}"


@dataclass(slots=True)
class _FakeRenameClient:
    gateway: _FakeGatewayClient

    def move_path(self, source_key: str, destination_key: str) -> None:
        matches = [
            (bucket, key)
            for bucket, key in self.gateway.objects
            if key == source_key or key.startswith(f"{source_key}/")
        ]
        if not matches:
            raise AssertionError(f"missing rename source {source_key}")
        for bucket, key in matches:
            suffix = key.removeprefix(source_key)
            self.gateway.objects[(bucket, f"{destination_key}{suffix}")] = self.gateway.objects.pop(
                (bucket, key)
            )


def _gateway_filesystem(client: _FakeGatewayClient) -> JuiceFsGatewayVolumeFilesystem:
    return JuiceFsGatewayVolumeFilesystem(
        client=client,
        rename_client=_FakeRenameClient(client),
    )


class _FailingDeleteFilesystem(LocalVolumeFilesystem):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail_delete = True

    def delete_volume(self, namespace: VolumeNamespace) -> None:
        if self.fail_delete:
            raise OSError("storage delete unavailable")
        super().delete_volume(namespace)


def _set_volume_checkpoint(services: ApiServices, name: str) -> None:
    with services.context.database.session() as session:
        row = session.scalars(select(VolumeTable).where(VolumeTable.name == name)).one()
        row.size_bytes = 17
        row.metered_at = datetime.now(UTC) - timedelta(seconds=5)
