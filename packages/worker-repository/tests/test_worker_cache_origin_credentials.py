from __future__ import annotations

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.images import ImageRepository
from shared.identity import TokenKind, WorkspaceStorageConfig
from shared.image_building.records import ImageRecord
from storage.service import ObjectStorage
from storage_client.s3 import S3PresignedUpload
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.image_lifecycle import ImageRegistryStore
from worker.origin_access import (
    CacheOriginCredentialRequest,
    ImageArchiveUploadCredentialRequest,
)
from worker_repository.origin_credentials import (
    CacheOriginCredentialConfig,
    WorkerCacheOriginCredentialService,
)


def test_cache_origin_credentials_vend_only_archive_url(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = control.upsert_workspace(
        "workspace-a",
        storage=WorkspaceStorageConfig(
            backend="s3",
            bucket="workspace-bucket",
            config={
                "endpoint_url": "https://workspace-storage.local",
                "region": "us-test-1",
                "access_key": "workspace-ak",
                "secret_key": "workspace-sk",
                "force_path_style": True,
            },
        ),
    )
    signer = _FakePresigner()
    coordinates = ObjectStorage(
        isolated_services.context,
        object_client=isolated_services.object_storage.object_client,
        default_bucket=isolated_services.object_storage.default_bucket,
        allowed_buckets=("archive-bucket",),
    )
    archive_sha256 = "a" * 64
    archive = coordinates.reserve_for_workspace(
        workspace_id=workspace.id,
        bucket="archive-bucket",
        key="image-123.rclip",
        size=1024,
        sha256=archive_sha256,
        content_type="application/x-tar",
        metadata={"kind": "image-build-staging"},
    )
    with isolated_services.context.database.session() as session:
        ImageRepository(session).upsert(
            ImageRecord(
                workspace_id=workspace.id,
                image_id="image-123",
                archive_object_id=archive.id,
                archive_object_key=archive.key,
                archive_size_bytes=archive.size,
                archive_sha256=archive.sha256,
            )
        )
    service = WorkerCacheOriginCredentialService(
        isolated_services,
        config=CacheOriginCredentialConfig(
            image_registry_store=ImageRegistryStore.S3,
            image_archive_bucket="archive-bucket",
            image_archive_presign_seconds=900,
        ),
        object_store_client=signer,
        object_coordinates=coordinates,
    )

    credentials = service.vend(
        CacheOriginCredentialRequest(
            workspace_id=workspace.id,
            container_id="container-123",
            image_id="image-123",
        ),
        principal=WorkerCredentialPrincipal(
            workspace_id="infrastructure-worker",
            token_kind=TokenKind.Worker,
        ),
    )

    assert credentials.ok
    physical_download_key = f"workspaces/{workspace.id}/archive-bucket/image-123.rclip"
    physical_bucket = coordinates.physical_bucket("archive-bucket")
    assert credentials.image_archive_url == (
        f"https://signed/{physical_bucket}/{physical_download_key}?ttl=900"
    )
    assert credentials.archive_object_id == archive.id
    assert credentials.archive_size_bytes == 1024
    assert credentials.archive_sha256 == archive_sha256
    assert signer.calls == [(physical_bucket, physical_download_key, 900)]

    upload = service.vend_upload(
        ImageArchiveUploadCredentialRequest(
            workspace_id=workspace.id,
            build_id="build-1",
            container_id="build-container-1",
            image_id="image-123",
            upload_capability="a" * 32,
            archive_size_bytes=1024,
            archive_sha256=archive_sha256,
        ),
        principal=WorkerCredentialPrincipal(
            workspace_id="infrastructure-worker",
            token_kind=TokenKind.Worker,
        ),
        archive_object_id="reserved-upload-object",
    )

    assert upload.ok
    assert upload.archive_object_id == "reserved-upload-object"
    assert upload.object_key == "image-builds/build-1/image-123.rclip"
    assert upload.upload_url
    assert upload.upload_headers["x-amz-meta-artifact-sha256"] == archive_sha256
    assert signer.calls[-1] == (
        physical_bucket,
        f"workspaces/{workspace.id}/archive-bucket/{upload.object_key}",
        900,
    )

    other = service.vend(
        CacheOriginCredentialRequest(
            workspace_id="workspace-b",
            container_id="container-456",
            image_id="image-123",
        ),
        principal=WorkerCredentialPrincipal(
            workspace_id="infrastructure-worker",
            token_kind=TokenKind.Worker,
        ),
    )

    assert other.ok
    assert not other.image_archive_url
    assert not other.archive_object_id


def test_image_archive_vending_requires_injected_lifespan_signer(
    isolated_services: ApiServices,
) -> None:
    service = WorkerCacheOriginCredentialService(
        isolated_services,
        config=CacheOriginCredentialConfig(
            image_registry_store=ImageRegistryStore.S3,
            image_archive_bucket="archive-bucket",
        ),
    )

    credentials = service.vend(
        CacheOriginCredentialRequest(
            workspace_id="workspace-1",
            container_id="container-1",
            image_id="image-123",
        ),
        principal=WorkerCredentialPrincipal(
            workspace_id="workspace-1",
            token_kind=TokenKind.Worker,
        ),
    )

    assert not credentials.ok
    assert credentials.error_msg == "image archive signer is unavailable"


def test_image_archive_presign_failures_are_sanitized(
    isolated_services: ApiServices,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("workspace-1")
    coordinates = ObjectStorage(
        isolated_services.context,
        object_client=isolated_services.object_storage.object_client,
        default_bucket=isolated_services.object_storage.default_bucket,
        allowed_buckets=("archive-bucket",),
    )
    archive = coordinates.reserve_for_workspace(
        workspace_id=workspace.id,
        bucket="archive-bucket",
        key="image-123.rclip",
        size=1024,
        sha256="a" * 64,
        content_type="application/x-tar",
    )
    with isolated_services.context.database.session() as session:
        ImageRepository(session).upsert(
            ImageRecord(
                workspace_id=workspace.id,
                image_id="image-123",
                archive_object_id=archive.id,
                archive_object_key=archive.key,
                archive_size_bytes=archive.size,
                archive_sha256=archive.sha256,
            )
        )
    service = WorkerCacheOriginCredentialService(
        isolated_services,
        config=CacheOriginCredentialConfig(
            image_registry_store=ImageRegistryStore.S3,
            image_archive_bucket="archive-bucket",
        ),
        object_store_client=_FailingPresigner(),
        object_coordinates=_FailingPresigner(),
    )
    principal = WorkerCredentialPrincipal(
        workspace_id=workspace.id,
        token_kind=TokenKind.Worker,
    )

    download = service.vend(
        CacheOriginCredentialRequest(
            workspace_id=workspace.id,
            container_id="container-1",
            image_id="image-123",
        ),
        principal=principal,
    )
    upload = service.vend_upload(
        ImageArchiveUploadCredentialRequest(
            workspace_id=workspace.id,
            build_id="build-1",
            container_id="container-1",
            image_id="image-123",
            upload_capability="a" * 32,
            archive_size_bytes=1024,
            archive_sha256="a" * 64,
        ),
        principal=principal,
        archive_object_id="reserved-upload-object",
    )

    assert not download.ok
    assert download.error_msg == "image archive download URL is unavailable"
    assert not upload.ok
    assert upload.error_msg == "image archive upload URL is unavailable"
    assert "sensitive" not in download.model_dump_json()
    assert "sensitive" not in upload.model_dump_json()


def test_cache_origin_credentials_restrict_private_worker_workspace(
    isolated_services: ApiServices,
) -> None:
    service = WorkerCacheOriginCredentialService(isolated_services)

    non_worker = service.vend(
        CacheOriginCredentialRequest(workspace_id="workspace-a", container_id="container-a"),
        principal=WorkerCredentialPrincipal(
            workspace_id="workspace-a",
            token_kind=TokenKind.Workspace,
        ),
    )
    assert not non_worker.ok
    assert non_worker.error_msg == "worker token is required"

    missing_workspace = service.vend(
        CacheOriginCredentialRequest(workspace_id="", container_id="container-a"),
        principal=WorkerCredentialPrincipal(
            workspace_id="workspace-a",
            token_kind=TokenKind.WorkerPrivate,
        ),
    )
    assert not missing_workspace.ok
    assert missing_workspace.error_msg == "workspace id is required"

    wrong_workspace = service.vend(
        CacheOriginCredentialRequest(workspace_id="workspace-b", container_id="container-a"),
        principal=WorkerCredentialPrincipal(
            workspace_id="workspace-a",
            token_kind=TokenKind.WorkerPrivate,
        ),
    )
    assert not wrong_workspace.ok
    assert "cannot request credentials" in wrong_workspace.error_msg


class _FakePresigner:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, int]] = []

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        self.calls.append((bucket, key, expires_seconds))
        return f"https://signed/{bucket}/{key}?ttl={expires_seconds}"

    def generate_presigned_put(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3PresignedUpload:
        self.calls.append((bucket, key, expires_seconds))
        return S3PresignedUpload(
            url=f"https://signed/{bucket}/{key}?ttl={expires_seconds}",
            headers={
                "content-length": str(content_length),
                "content-type": content_type,
                **{f"x-amz-meta-{name}": value for name, value in (metadata or {}).items()},
            },
        )

    def physical_key_for_workspace(
        self,
        workspace_id: str,
        *,
        bucket: str,
        key: str,
    ) -> str:
        return f"workspaces/{workspace_id}/{bucket}/{key}"

    def physical_bucket(self, bucket: str) -> str:
        return bucket


class _FailingPresigner:
    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        del key, bucket, expires_seconds
        raise RuntimeError("sensitive endpoint and request details")

    def generate_presigned_put(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3PresignedUpload:
        del key, bucket, expires_seconds, content_length, content_type, metadata
        raise RuntimeError("sensitive endpoint and request details")

    def physical_key_for_workspace(
        self,
        workspace_id: str,
        *,
        bucket: str,
        key: str,
    ) -> str:
        return f"workspaces/{workspace_id}/{bucket}/{key}"

    def physical_bucket(self, bucket: str) -> str:
        return bucket
