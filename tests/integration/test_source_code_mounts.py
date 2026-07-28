from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from execution.mounts import (
    container_resource_mounts,
    container_resource_mounts_require_workspace_storage,
    platform_volume_local_path,
    source_code_mounts,
)
from lazycloud.session.source_sync import SOURCE_PACKAGE_BUCKET
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.state import SchedulerWorkerRequest
from shared.container_requests import (
    WORKER_USER_CODE_VOLUME,
    RequestMount,
)
from shared.errors import UpstreamUnavailableError
from shared.identity import WorkspaceStorageConfig
from storage.service import ObjectStorage
from storage_client.s3 import S3ObjectInfo
from worker.events import ContainerRequestContext
from worker.execution import stub_code_cache_key
from worker.source_code import SourceCodePackageMaterializer


@dataclass
class _ObjectClient:
    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    presigned: list[tuple[str, str, int]] = field(default_factory=list)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        target_bucket = bucket or "default"
        self.objects[(target_bucket, key)] = data
        return S3ObjectInfo(bucket=target_bucket, key=key, size=len(data))

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        return self.put_bytes(key, Path(source).read_bytes(), bucket=bucket)

    def read_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        return self.objects[(bucket or "default", key)]

    def download_file(
        self,
        key: str,
        target: str | Path,
        *,
        bucket: str | None = None,
    ) -> S3ObjectInfo:
        target_bucket = bucket or "default"
        payload = self.read_bytes(key, bucket=target_bucket)
        Path(target).write_bytes(payload)
        return S3ObjectInfo(bucket=target_bucket, key=key, size=len(payload))

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        data = self.read_bytes(key, bucket=bucket)
        return S3ObjectInfo(bucket=bucket or "default", key=key, size=len(data))

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        return (bucket or "default", key) in self.objects

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        self.presigned.append((bucket or "default", key, expires_seconds))
        return f"https://objects.test/{bucket}/{key}"

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str:
        _ = content_length, content_type
        return f"https://objects.test/{bucket}/{key}?expires={expires_seconds}"

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        self.objects.pop((bucket or "default", key), None)


@dataclass
class _Scheduler:
    requests: list[SchedulerWorkerRequest] = field(default_factory=list)

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: object | None = None,
    ) -> SchedulerContainerSubmitResult:
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=getattr(request, "container_id", "container"),
        )


def test_source_code_mounts_presign_for_the_source_workspace(
    isolated_services: ApiServices,
) -> None:
    object_client = _ObjectClient()
    object_storage = ObjectStorage(isolated_services.context, object_client=object_client)
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("source-owner")
    record = object_storage.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/cross-workspace.zip",
        data=_zip_bytes({"main.py": "print('hi')\n"}),
        content_type="application/zip",
    )

    mounts = source_code_mounts(
        context=isolated_services.context,
        object_storage=object_storage,
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        object_id=record.id,
    )
    physical_key = object_storage.physical_key_for_record(record)
    physical_bucket = object_storage.physical_bucket(SOURCE_PACKAGE_BUCKET)

    assert mounts[0].source_download_url == (
        f"https://objects.test/{physical_bucket}/{physical_key}"
    )
    assert object_client.presigned == [(physical_bucket, physical_key, 900)]


def test_object_storage_deletes_only_the_target_workspace_objects(
    isolated_services: ApiServices,
) -> None:
    object_client = _ObjectClient()
    object_storage = ObjectStorage(isolated_services.context, object_client=object_client)
    control = ControlPlaneService(isolated_services.context)
    target = control.upsert_workspace("object-target")
    retained = control.upsert_workspace("object-retained")
    target_record = object_storage.put_bytes_for_workspace(
        workspace_id=target.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/target.zip",
        data=b"target",
    )
    retained_record = object_storage.put_bytes_for_workspace(
        workspace_id=retained.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/retained.zip",
        data=b"retained",
    )
    target_physical_key = object_storage.physical_key_for_record(target_record)
    retained_physical_key = object_storage.physical_key_for_record(retained_record)
    physical_bucket = object_storage.physical_bucket(SOURCE_PACKAGE_BUCKET)

    assert object_storage.delete_workspace_objects(target.id) == 1

    assert (physical_bucket, target_physical_key) not in object_client.objects
    assert (physical_bucket, retained_physical_key) in object_client.objects
    assert object_storage.list_for_workspace(workspace_id=target.id) == []
    assert (
        object_storage.get_by_id_for_workspace(
            retained_record.id,
            workspace_id=retained.id,
        ).id
        == retained_record.id
    )
    assert target_record.id != retained_record.id


def test_object_storage_deletes_each_workspace_physical_object_independently(
    isolated_services: ApiServices,
) -> None:
    object_client = _ObjectClient()
    object_storage = ObjectStorage(isolated_services.context, object_client=object_client)
    control = ControlPlaneService(isolated_services.context)
    first = control.upsert_workspace("object-first")
    last = control.upsert_workspace("object-last")
    key = "sources/shared.zip"
    first_record = object_storage.put_bytes_for_workspace(
        workspace_id=first.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key=key,
        data=b"shared",
    )
    last_record = object_storage.put_bytes_for_workspace(
        workspace_id=last.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key=key,
        data=b"shared",
    )
    first_physical_key = object_storage.physical_key_for_record(first_record)
    last_physical_key = object_storage.physical_key_for_record(last_record)
    physical_bucket = object_storage.physical_bucket(SOURCE_PACKAGE_BUCKET)

    assert object_storage.delete_workspace_objects(first.id) == 1
    assert not object_client.exists(first_physical_key, bucket=physical_bucket)
    assert object_client.exists(last_physical_key, bucket=physical_bucket)
    assert object_storage.list_for_workspace(workspace_id=first.id) == []
    assert (
        object_storage.get_by_id_for_workspace(last_record.id, workspace_id=last.id).id
        == last_record.id
    )

    assert object_storage.delete_workspace_objects(last.id) == 1
    assert not object_client.exists(last_physical_key, bucket=physical_bucket)
    assert first_record.id != last_record.id


def test_object_storage_preserves_metadata_when_physical_delete_is_not_confirmed(
    isolated_services: ApiServices,
) -> None:
    object_client = _StickyDeleteObjectClient()
    object_storage = ObjectStorage(isolated_services.context, object_client=object_client)
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("object-sticky")
    record = object_storage.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/sticky.zip",
        data=b"sticky",
    )
    physical_key = object_storage.physical_key_for_record(record)

    with pytest.raises(UpstreamUnavailableError, match="object deletion was not confirmed"):
        object_storage.delete_workspace_objects(workspace.id)

    assert (
        object_storage.get_by_id_for_workspace(record.id, workspace_id=workspace.id).id == record.id
    )
    assert object_client.exists(physical_key, bucket=object_storage.physical_bucket(record.bucket))


class _StickyDeleteObjectClient(_ObjectClient):
    def delete(self, key: str, *, bucket: str | None = None) -> None:
        _ = (key, bucket)


def test_container_resource_mounts_require_workspace_storage_when_workspace_has_bucket(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    unprovisioned = control.upsert_workspace("mount-storage-unprovisioned")
    mounts = container_resource_mounts(
        context=isolated_services.context,
        object_storage=isolated_services.object_storage,
        workspace_id=unprovisioned.id,
        workspace_name=unprovisioned.name,
        object_id="",
        stub_id="stub-1",
        container_id="ctr-1",
        volumes=[],
    )

    # The artifact mount always needs workspace storage, and there is no fallback
    # tier, so an unprovisioned workspace must fail rather than mount local disk.
    with pytest.raises(UpstreamUnavailableError, match="no storage provisioned"):
        container_resource_mounts_require_workspace_storage(
            context=isolated_services.context,
            workspace_id=unprovisioned.id,
            mounts=mounts,
        )

    control.set_workspace_storage(
        unprovisioned.id,
        WorkspaceStorageConfig(
            backend="s3",
            bucket="workspace-bucket",
            config={
                "endpoint_url": "http://object-store:9000",
                "region": "us-east-1",
                "access_key": "access",
                "secret_key": "secret",
                "force_path_style": True,
            },
        ),
    )

    assert container_resource_mounts_require_workspace_storage(
        context=isolated_services.context,
        workspace_id=unprovisioned.id,
        mounts=mounts,
    )

    # A platform volume lives in the workspace's own storage, so it needs the
    # mount the same way an artifact does, even with no artifact mount present.
    volume_only_mounts = container_resource_mounts(
        context=isolated_services.context,
        object_storage=isolated_services.object_storage,
        workspace_id=unprovisioned.id,
        workspace_name=unprovisioned.name,
        object_id="",
        stub_id="",
        container_id="ctr-volume-only",
        volumes=[{"id": "canonical", "mount_path": "/volumes/canonical"}],
    )
    assert container_resource_mounts_require_workspace_storage(
        context=isolated_services.context,
        workspace_id=unprovisioned.id,
        mounts=volume_only_mounts,
    )


@pytest.mark.parametrize(
    ("workspace_name", "volume_id"),
    [
        ("../workspace", "volume-1"),
        ("workspace-1", "../volume"),
        ("workspace/other", "volume-1"),
        ("workspace-1", "volume\\other"),
    ],
)
def test_platform_volume_local_path_rejects_namespace_traversal(
    workspace_name: str,
    volume_id: str,
) -> None:
    with pytest.raises(ValueError, match=r"unsafe (workspace_name|volume_id)"):
        platform_volume_local_path(workspace_name=workspace_name, volume_id=volume_id)


def test_worker_source_materializer_extracts_isolated_container_workspaces(tmp_path: Path) -> None:
    data = _zip_bytes({"main.py": "print('hello')\n", "pkg/__init__.py": ""})
    digest = hashlib.sha256(data).hexdigest()
    archive_path = tmp_path / "source.zip"
    archive_path.write_bytes(data)
    materializer = SourceCodePackageMaterializer(
        cache_root=tmp_path / "cache",
        workspace_root=tmp_path / "workspaces",
    )
    first = materializer.materialize(
        ContainerRequestContext(
            container_id="source-sdk1-a",
            workspace_id="workspace-team",
            workspace_name="team",
        ),
        _mount(digest, archive_path),
    )
    Path(first.workspace_path, "main.py").write_text("mutated\n", encoding="utf-8")

    second = materializer.materialize(
        ContainerRequestContext(
            container_id="source-sdk1-b",
            workspace_id="workspace-team",
            workspace_name="team-renamed",
        ),
        _mount(digest, archive_path),
    )

    assert first.workspace_path != second.workspace_path
    assert Path(first.cache_path, ".source-cache-ready").is_file()
    assert Path(first.cache_path, "main.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert Path(second.workspace_path, "main.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert second.cache_hit


def test_worker_source_materializer_purges_only_requested_workspace_objects(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    materializer = SourceCodePackageMaterializer(cache_root=cache_root)
    target = cache_root / stub_code_cache_key("workspace-a", "object-1")
    other_object = cache_root / stub_code_cache_key("workspace-a", "object-2")
    other_workspace = cache_root / stub_code_cache_key("workspace-b", "object-1")
    for path in (target, other_object, other_workspace):
        path.mkdir(parents=True)
        (path / ".source-cache-ready").write_text("ok", encoding="utf-8")

    result = materializer.purge("workspace-a", ["object-1", "object-1"])

    assert result.workspace_id == "workspace-a"
    assert result.removed_paths == [str(target)]
    assert not target.exists()
    assert other_object.is_dir()
    assert other_workspace.is_dir()


def _mount(digest: str, archive_path: Path) -> RequestMount:
    return RequestMount(
        local_path=str(archive_path),
        mount_path=WORKER_USER_CODE_VOLUME,
        source_object_id="obj-source",
        source_sha256=digest,
    )


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return buffer.getvalue()
