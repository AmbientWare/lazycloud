"""Server-side worker origin credential services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.contracts import ContractModel
from shared.identity import TokenKind
from shared.image_building.records import ImageRecord
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
    image_build_archive_staging_key,
)

DEFAULT_IMAGE_ARCHIVE_PRESIGN_SECONDS = 15 * 60


class CacheOriginCredentialConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    image_registry_store: ImageRegistryStore = ImageRegistryStore.Local
    image_archive_bucket: str = ""
    image_archive_presign_seconds: int = DEFAULT_IMAGE_ARCHIVE_PRESIGN_SECONDS
    image_archive_extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION

    @field_validator("image_archive_presign_seconds")
    @classmethod
    def presign_seconds_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "image archive presign seconds must be positive"
            raise ValueError(msg)
        return value

    @property
    def image_archive_available(self) -> bool:
        return (
            self.image_registry_store is ImageRegistryStore.S3 and self.image_archive_bucket != ""
        )


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
    ) -> PresignedUpload: ...


class WorkspaceObjectCoordinates(Protocol):
    def physical_key_for_workspace(
        self,
        workspace_id: str,
        *,
        bucket: str,
        key: str,
    ) -> str: ...

    def physical_bucket(self, bucket: str) -> str: ...


class WorkerOriginCredentialImageLookup(Protocol):
    def get_image_metadata(
        self,
        image_id: str,
        *,
        workspace_id: str,
    ) -> ImageRecord | None: ...


class WorkerOriginCredentialServices(Protocol):
    @property
    def images(self) -> WorkerOriginCredentialImageLookup: ...


@dataclass(slots=True)
class WorkerCacheOriginCredentialService:
    services: WorkerOriginCredentialServices | None = None
    config: CacheOriginCredentialConfig = field(default_factory=CacheOriginCredentialConfig)
    object_store_client: PresignedPutClient | None = None
    object_coordinates: WorkspaceObjectCoordinates | None = None

    def vend(
        self,
        request: CacheOriginCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
    ) -> CacheOriginCredentials:
        denial = self._authorization_denial(request, principal)
        if denial:
            return CacheOriginCredentials.denied(denial)
        if (
            request.image_id
            and self.config.image_archive_available
            and (self.object_store_client is None or self.object_coordinates is None)
        ):
            return CacheOriginCredentials.denied("image archive signer is unavailable")

        archive = self._image_archive_credentials(
            request.image_id,
            workspace_id=request.workspace_id,
        )
        if archive.error:
            return CacheOriginCredentials.denied(archive.error)
        return CacheOriginCredentials(
            archive_object_id=archive.object_id,
            image_archive_url=archive.url,
            archive_size_bytes=archive.size_bytes,
            archive_sha256=archive.sha256,
        )

    def vend_upload(
        self,
        request: ImageArchiveUploadCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
        archive_object_id: str,
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
        if not self.config.image_archive_available:
            return ImageArchiveUploadCredentials.denied("image archive storage is not configured")
        if not request.image_id:
            return ImageArchiveUploadCredentials.denied("image id is required")
        if not archive_object_id:
            return ImageArchiveUploadCredentials.denied("image archive reservation is unavailable")
        if self.object_store_client is None or self.object_coordinates is None:
            return ImageArchiveUploadCredentials.denied("image archive signer is unavailable")

        _logical_bucket, object_key = self.upload_location(request)
        try:
            physical_bucket, physical_key = self._physical_archive_location(
                workspace_id=request.workspace_id,
                object_key=object_key,
            )
            upload = self.object_store_client.generate_presigned_put(
                physical_key,
                bucket=physical_bucket,
                expires_seconds=self.config.image_archive_presign_seconds,
                content_length=request.archive_size_bytes,
                content_type=request.content_type,
                metadata={"artifact-sha256": request.archive_sha256},
            )
        except Exception:
            return ImageArchiveUploadCredentials.denied("image archive upload URL is unavailable")
        return ImageArchiveUploadCredentials(
            archive_object_id=archive_object_id,
            bucket=self.config.image_archive_bucket,
            object_key=object_key,
            upload_url=upload.url,
            upload_headers=upload.headers,
        )

    def upload_location(
        self,
        request: ImageArchiveUploadCredentialRequest,
    ) -> tuple[str, str]:
        return (
            self.config.image_archive_bucket,
            image_build_archive_staging_key(
                request.image_id,
                request.build_id,
                extension=self.config.image_archive_extension,
            ),
        )

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
        if not self.config.image_archive_available or not image_id:
            return _ImageArchiveCredentials()

        image = self._services().images.get_image_metadata(
            image_id,
            workspace_id=workspace_id,
        )
        if image is None or not image.archive_object_key:
            return _ImageArchiveCredentials()
        object_key = image.archive_object_key
        try:
            if self.object_store_client is None or self.object_coordinates is None:
                return _ImageArchiveCredentials()
            physical_bucket, physical_key = self._physical_archive_location(
                workspace_id=workspace_id,
                object_key=object_key,
            )
            url = self.object_store_client.generate_presigned_get_url(
                physical_key,
                bucket=physical_bucket,
                expires_seconds=self.config.image_archive_presign_seconds,
            )
        except Exception:
            return _ImageArchiveCredentials(error="image archive download URL is unavailable")
        return _ImageArchiveCredentials(
            object_id=image.archive_object_id,
            url=url,
            size_bytes=image.archive_size_bytes,
            sha256=image.archive_sha256,
        )

    def _physical_archive_location(
        self,
        *,
        workspace_id: str,
        object_key: str,
    ) -> tuple[str, str]:
        coordinates = self.object_coordinates
        if coordinates is None:
            raise RuntimeError("image archive object coordinates are unavailable")
        bucket = self.config.image_archive_bucket
        return (
            coordinates.physical_bucket(bucket),
            coordinates.physical_key_for_workspace(
                workspace_id,
                bucket=bucket,
                key=object_key,
            ),
        )

    def _services(self) -> WorkerOriginCredentialServices:
        if self.services is None:
            msg = "worker origin credential service requires control-plane services"
            raise RuntimeError(msg)
        return self.services


class _ImageArchiveCredentials(ContractModel):
    object_id: str = ""
    url: str = ""
    size_bytes: int = 0
    sha256: str = ""
    error: str = ""
