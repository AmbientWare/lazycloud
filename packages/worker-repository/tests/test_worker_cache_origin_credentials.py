from __future__ import annotations

from base64 import b64encode

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.images import ImageArchiveRepository, ImageRepository
from shared.identity import TokenKind
from shared.image_building.records import ImageArchiveRecord, ImageRecord
from storage.image_archive import ImageArchiveSettings
from storage_client.s3 import S3PresignedUpload
from tests.domain_fixtures import owned_workspace
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.image_lifecycle import ImageRegistryStore
from worker.origin_access import (
    CacheOriginCredentialRequest,
    ImageArchiveUploadCredentialRequest,
    ImageRegistryCredentials,
)
from worker_repository.origin_credentials import (
    CacheOriginCredentialConfig,
    WorkerCacheOriginCredentialService,
)

ARCHIVE_SHA256 = "a" * 64
ARCHIVE_KEY = "image-archives/image-123.rclip"
MANIFEST_DIGEST = "sha256:" + "b" * 64
REGISTRY_REPOSITORY = "registry.example.com/workloads"
REGISTRY_REF = f"{REGISTRY_REPOSITORY}@{MANIFEST_DIGEST}"


def _archive_settings() -> ImageArchiveSettings:
    return ImageArchiveSettings(
        bucket="archive-bucket",
        presign_seconds=900,
    )


def _s3_config() -> CacheOriginCredentialConfig:
    return CacheOriginCredentialConfig(
        image_registry_store=ImageRegistryStore.S3,
        workload_image_registry_repository=REGISTRY_REPOSITORY,
    )


def _registry_credentials(registry: str) -> ImageRegistryCredentials:
    return ImageRegistryCredentials(registry=registry)


def _publish_archive(services: ApiServices, *, workspace_id: str) -> ImageArchiveRecord:
    with services.context.database.session() as session:
        archive, _ = ImageArchiveRepository(session).reserve(
            "image-123",
            bucket="archive-bucket",
            object_key=ARCHIVE_KEY,
            size_bytes=1024,
            sha256=ARCHIVE_SHA256,
            registry_ref=REGISTRY_REF,
            manifest_digest=MANIFEST_DIGEST,
            architecture="amd64",
            format_version=2,
        )
        ImageRepository(session).upsert(
            ImageRecord(workspace_id=workspace_id, image_id="image-123")
        )
    return archive


def test_archive_download_is_signed_only_for_an_authorized_workspace(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "workspace-a")
    sibling = owned_workspace(control, "workspace-b")
    archive = _publish_archive(isolated_services, workspace_id=workspace.id)
    signer = _FakePresigner()
    service = WorkerCacheOriginCredentialService(
        isolated_services,
        config=_s3_config(),
        object_store_client=signer,
        archive_settings=_archive_settings(),
        registry_credentials=_registry_credentials,
    )
    principal = WorkerCredentialPrincipal(
        workspace_id="infrastructure-worker",
        token_kind=TokenKind.Worker,
    )

    credentials = service.vend(
        CacheOriginCredentialRequest(
            workspace_id=workspace.id,
            container_id="container-123",
            image_id="image-123",
        ),
        principal=principal,
    )

    assert credentials.ok
    physical_key = ARCHIVE_KEY
    assert credentials.image_archive_url == f"https://signed/archive-bucket/{physical_key}?ttl=900"
    assert credentials.archive_size_bytes == archive.size_bytes
    assert credentials.archive_sha256 == ARCHIVE_SHA256
    assert credentials.registry_ref == REGISTRY_REF
    assert signer.calls == [("archive-bucket", physical_key, 900)]

    # The archive row is global and its key carries no tenant component, so this
    # join is the entire download boundary.
    other = service.vend(
        CacheOriginCredentialRequest(
            workspace_id=sibling.id,
            container_id="container-456",
            image_id="image-123",
        ),
        principal=principal,
    )

    assert other.ok
    assert not other.image_archive_url
    assert signer.calls == [("archive-bucket", physical_key, 900)]


def test_archive_upload_binds_the_reserved_digest_and_skips_a_published_archive(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(ControlPlaneService(isolated_services.context), "workspace-a")
    archive = _publish_archive(isolated_services, workspace_id=workspace.id)
    signer = _FakePresigner()
    service = WorkerCacheOriginCredentialService(
        isolated_services,
        config=_s3_config(),
        object_store_client=signer,
        archive_settings=_archive_settings(),
        registry_credentials=_registry_credentials,
    )
    request = ImageArchiveUploadCredentialRequest(
        workspace_id=workspace.id,
        build_id="build-1",
        container_id="build-container-1",
        image_id="image-123",
        upload_capability="a" * 32,
        archive_size_bytes=1024,
        archive_sha256=ARCHIVE_SHA256,
        registry_ref=REGISTRY_REF,
        manifest_digest=MANIFEST_DIGEST,
        architecture="amd64",
        format_version=2,
    )
    principal = WorkerCredentialPrincipal(
        workspace_id="infrastructure-worker",
        token_kind=TokenKind.Worker,
    )

    upload = service.vend_upload(
        request,
        principal=principal,
        archive=archive,
        upload_required=True,
    )

    assert upload.ok
    assert upload.object_key == ARCHIVE_KEY
    assert upload.upload_url
    assert upload.upload_headers["x-amz-meta-artifact-sha256"] == ARCHIVE_SHA256
    # Signed into the URL, so a compromised build container cannot poison the one
    # archive every tenant resolving this image id shares.
    assert (
        upload.upload_headers["x-amz-checksum-sha256"]
        == b64encode(bytes.fromhex(ARCHIVE_SHA256)).decode()
    )
    assert signer.calls[-1] == ("archive-bucket", ARCHIVE_KEY, 900)

    published = service.vend_upload(
        request,
        principal=principal,
        archive=archive,
        upload_required=False,
    )

    assert published.ok
    assert not published.upload_url
    assert not published.upload_headers
    assert published.object_key == ARCHIVE_KEY
    assert published.archive_size_bytes == archive.size_bytes
    assert published.archive_sha256 == ARCHIVE_SHA256


def test_image_archive_vending_requires_injected_lifespan_signer(
    isolated_services: ApiServices,
) -> None:
    service = WorkerCacheOriginCredentialService(
        isolated_services,
        config=_s3_config(),
        archive_settings=_archive_settings(),
        registry_credentials=_registry_credentials,
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
    workspace = owned_workspace(ControlPlaneService(isolated_services.context), "workspace-1")
    archive = _publish_archive(isolated_services, workspace_id=workspace.id)
    service = WorkerCacheOriginCredentialService(
        isolated_services,
        config=_s3_config(),
        object_store_client=_FailingPresigner(),
        archive_settings=_archive_settings(),
        registry_credentials=_registry_credentials,
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
            archive_sha256=ARCHIVE_SHA256,
            registry_ref=REGISTRY_REF,
            manifest_digest=MANIFEST_DIGEST,
            architecture="amd64",
            format_version=2,
        ),
        principal=principal,
        archive=archive,
        upload_required=True,
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
        checksum_sha256: str = "",
    ) -> S3PresignedUpload:
        self.calls.append((bucket, key, expires_seconds))
        return S3PresignedUpload(
            url=f"https://signed/{bucket}/{key}?ttl={expires_seconds}",
            headers={
                "content-length": str(content_length),
                "content-type": content_type,
                **({"x-amz-checksum-sha256": checksum_sha256} if checksum_sha256 else {}),
                **{f"x-amz-meta-{name}": value for name, value in (metadata or {}).items()},
            },
        )


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
        checksum_sha256: str = "",
    ) -> S3PresignedUpload:
        del key, bucket, expires_seconds, content_length, content_type, metadata, checksum_sha256
        raise RuntimeError("sensitive endpoint and request details")
