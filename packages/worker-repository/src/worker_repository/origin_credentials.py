"""Server-side worker origin credential services."""

from __future__ import annotations

import logging
from base64 import b64encode
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.contracts import ContractModel
from shared.identity import TokenKind
from shared.image_building.constants import image_archive_object_key
from shared.image_building.credentials import registry_host_for_image
from shared.image_building.records import ImageArchiveRecord
from storage.image_archive import ImageArchiveSettings
from worker.credential_payloads import WORKER_TOKEN_KINDS, WorkerCredentialPrincipal
from worker.image_lifecycle import (
    DEFAULT_IMAGE_ARCHIVE_EXTENSION,
    ImageRegistryStore,
)
from worker.origin_access import (
    CacheOriginCredentialRequest,
    CacheOriginCredentials,
    ImageArchiveUploadCredentialRequest,
    ImageArchiveUploadCredentials,
    ImageRegistryCredentials,
)

LOGGER = logging.getLogger(__name__)


class CacheOriginCredentialConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_",
        extra="ignore",
        populate_by_name=True,
    )

    image_registry_store: ImageRegistryStore = ImageRegistryStore.Local
    image_archive_extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION
    workload_image_registry_repository: str = ""


class PresignedGetUrlClient(Protocol):
    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str: ...


@runtime_checkable
class PresignedUpload(Protocol):
    url: str
    headers: dict[str, str]


@runtime_checkable
class PresignedPutClient(PresignedGetUrlClient, Protocol):
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
    ) -> PresignedUpload: ...


class WorkerOriginCredentialImageLookup(Protocol):
    def get_authorized_image_archive(
        self,
        image_id: str,
        *,
        workspace_id: str,
    ) -> ImageArchiveRecord | None: ...


class WorkerOriginCredentialServices(Protocol):
    @property
    def images(self) -> WorkerOriginCredentialImageLookup: ...


class ImageRegistryCredentialIssuer(Protocol):
    def __call__(self, registry: str) -> ImageRegistryCredentials: ...


@dataclass(slots=True)
class WorkerCacheOriginCredentialService:
    services: WorkerOriginCredentialServices | None = None
    config: CacheOriginCredentialConfig = field(default_factory=CacheOriginCredentialConfig)
    object_store_client: PresignedPutClient | None = None
    archive_settings: ImageArchiveSettings | None = None
    registry_credentials: ImageRegistryCredentialIssuer | None = None

    @property
    def image_archive_available(self) -> bool:
        settings = self.archive_settings
        return (
            self.config.image_registry_store is ImageRegistryStore.S3
            and settings is not None
            and settings.bucket != ""
        )

    def vend(
        self,
        request: CacheOriginCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
    ) -> CacheOriginCredentials:
        denial = self._authorization_denial(request, principal)
        if denial:
            return CacheOriginCredentials.denied(denial)
        if request.image_id and self.image_archive_available and self.object_store_client is None:
            return CacheOriginCredentials.denied("image archive signer is unavailable")

        archive = self._image_archive_credentials(
            request.image_id,
            workspace_id=request.workspace_id,
        )
        if archive.error:
            return CacheOriginCredentials.denied(archive.error)
        registry_repository = self.config.workload_image_registry_repository.strip()
        credentials: ImageRegistryCredentials | None = None
        if registry_repository:
            registry = registry_host_for_image(registry_repository)
            if not registry:
                return CacheOriginCredentials.denied(
                    "workload image registry repository is invalid"
                )
            if self.registry_credentials is not None:
                try:
                    credentials = self.registry_credentials(registry)
                except Exception:
                    LOGGER.warning(
                        "workload registry credentials could not be issued",
                        exc_info=True,
                    )
                    return CacheOriginCredentials.denied(
                        "workload image registry credentials are unavailable"
                    )
        return CacheOriginCredentials(
            image_archive_url=archive.url,
            archive_size_bytes=archive.size_bytes,
            archive_sha256=archive.sha256,
            registry_repository=registry_repository,
            registry_ref=archive.registry_ref,
            manifest_digest=archive.manifest_digest,
            architecture=archive.architecture,
            format_version=archive.format_version,
            registry_credentials=credentials,
        )

    def vend_upload(
        self,
        request: ImageArchiveUploadCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
        archive: ImageArchiveRecord,
        upload_required: bool,
    ) -> ImageArchiveUploadCredentials:
        denial = self._authorization_denial(
            CacheOriginCredentialRequest(
                workspace_id=request.workspace_id,
                container_id=request.container_id,
                stub_id=request.stub_id,
                image_id=request.image_id,
            ),
            principal,
        )
        if denial:
            return ImageArchiveUploadCredentials.denied(denial)
        settings = self.archive_settings
        if not self.image_archive_available or settings is None:
            return ImageArchiveUploadCredentials.denied("image archive storage is not configured")
        if not request.image_id:
            return ImageArchiveUploadCredentials.denied("image id is required")
        if archive.image_id != request.image_id:
            return ImageArchiveUploadCredentials.denied(
                "image archive reservation does not match the requested image"
            )
        if not upload_required:
            # Another build already published these bytes. The worker gets the
            # archive's identity so it can verify what it finds, and no URL, so it
            # cannot overwrite an archive other workspaces already resolve.
            return ImageArchiveUploadCredentials(
                bucket=archive.bucket,
                object_key=archive.object_key,
                archive_size_bytes=archive.size_bytes,
                archive_sha256=archive.sha256,
            )
        if self.object_store_client is None:
            return ImageArchiveUploadCredentials.denied("image archive signer is unavailable")

        try:
            upload = self.object_store_client.generate_presigned_put(
                settings.physical_key(archive.object_key),
                bucket=settings.bucket,
                expires_seconds=settings.presign_seconds,
                content_length=archive.size_bytes,
                content_type=request.content_type,
                metadata={"artifact-sha256": archive.sha256},
                checksum_sha256=b64encode(bytes.fromhex(archive.sha256)).decode(),
            )
        except Exception:
            LOGGER.warning("image archive upload URL could not be signed", exc_info=True)
            return ImageArchiveUploadCredentials.denied("image archive upload URL is unavailable")
        return ImageArchiveUploadCredentials(
            bucket=archive.bucket,
            object_key=archive.object_key,
            archive_size_bytes=archive.size_bytes,
            archive_sha256=archive.sha256,
            upload_url=upload.url,
            upload_headers=upload.headers,
        )

    def upload_location(
        self,
        request: ImageArchiveUploadCredentialRequest,
    ) -> tuple[str, str]:
        settings = self.archive_settings
        if settings is None:
            return ("", "")
        return (
            settings.bucket,
            image_archive_object_key(
                request.image_id,
                extension=self.config.image_archive_extension,
            ),
        )

    def upload_descriptor_denial(
        self,
        request: ImageArchiveUploadCredentialRequest,
    ) -> str:
        repository = self.config.workload_image_registry_repository.strip()
        if not repository:
            return "workload image registry is not configured"
        if not request.registry_ref.startswith(f"{repository}@"):
            return "image OCI descriptor does not belong to the workload registry"
        return ""

    def _authorization_denial(
        self,
        request: CacheOriginCredentialRequest,
        principal: WorkerCredentialPrincipal,
    ) -> str:
        if principal.token_kind not in WORKER_TOKEN_KINDS:
            return "worker token is required"
        if not request.workspace_id:
            return "workspace id is required"
        if principal.token_kind is TokenKind.WorkerPrivate and (
            not principal.workspace_id or principal.workspace_id != request.workspace_id
        ):
            return (
                "private worker token cannot request credentials for workspace "
                f"{request.workspace_id!r}"
            )
        return ""

    def _image_archive_credentials(
        self,
        image_id: str,
        *,
        workspace_id: str,
    ) -> _ImageArchiveCredentials:
        settings = self.archive_settings
        if not self.image_archive_available or settings is None or not image_id:
            return _ImageArchiveCredentials()

        # Deliberately the workspace-scoped lookup even though one row serves
        # every tenant: the physical key carries no tenant component, so this join
        # is the entire download boundary.
        archive = self._services().images.get_authorized_image_archive(
            image_id,
            workspace_id=workspace_id,
        )
        if archive is None:
            return _ImageArchiveCredentials()
        if archive.cleanup_claimed_at is not None:
            return _ImageArchiveCredentials(error="image archive is being reclaimed")
        repository = self.config.workload_image_registry_repository.strip()
        if not repository or not archive.registry_ref.startswith(f"{repository}@"):
            return _ImageArchiveCredentials(
                error="image OCI descriptor does not belong to the workload registry"
            )
        try:
            if self.object_store_client is None:
                return _ImageArchiveCredentials()
            url = self.object_store_client.generate_presigned_get_url(
                settings.physical_key(archive.object_key),
                bucket=settings.bucket,
                expires_seconds=settings.presign_seconds,
            )
        except Exception:
            LOGGER.warning("image archive download URL could not be signed", exc_info=True)
            return _ImageArchiveCredentials(error="image archive download URL is unavailable")
        return _ImageArchiveCredentials(
            url=url,
            size_bytes=archive.size_bytes,
            sha256=archive.sha256,
            registry_ref=archive.registry_ref,
            manifest_digest=archive.manifest_digest,
            architecture=archive.architecture,
            format_version=archive.format_version,
        )

    def _services(self) -> WorkerOriginCredentialServices:
        if self.services is None:
            msg = "worker origin credential service requires control-plane services"
            raise RuntimeError(msg)
        return self.services


class _ImageArchiveCredentials(ContractModel):
    url: str = ""
    size_bytes: int = 0
    sha256: str = ""
    registry_ref: str = ""
    manifest_digest: str = ""
    architecture: str = ""
    format_version: int = 0
    error: str = ""
