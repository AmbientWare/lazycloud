from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.contracts import ContractModel
from storage_client.s3 import S3ObjectStoreSettings

IMAGE_ARCHIVE_EXTENSION = "rclip"
IMAGE_ARCHIVE_DATA_EXTENSION = "clip"
DEFAULT_IMAGE_ARCHIVE_PRESIGN_SECONDS = 15 * 60


class ImageArchiveBackendSettings(ContractModel):
    """Complete coordinates for an archive store distinct from the primary store."""

    bucket: str = Field(min_length=1)
    endpoint_url: str | None = None
    presigned_endpoint_url: str | None = None
    region_name: str = "us-east-1"
    access_key_id: str = ""
    secret_access_key: SecretStr = SecretStr("")
    force_path_style: bool = False
    transfer_multipart_threshold_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    transfer_multipart_chunk_size_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    transfer_max_concurrency: int = Field(default=2, gt=0)

    def object_store_settings(self) -> S3ObjectStoreSettings:
        return S3ObjectStoreSettings(
            bucket=self.bucket,
            endpoint_url=self.endpoint_url,
            presigned_endpoint_url=self.presigned_endpoint_url,
            region_name=self.region_name,
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key.get_secret_value(),
            force_path_style=self.force_path_style,
            transfer_multipart_threshold_bytes=self.transfer_multipart_threshold_bytes,
            transfer_multipart_chunk_size_bytes=self.transfer_multipart_chunk_size_bytes,
            transfer_max_concurrency=self.transfer_max_concurrency,
        )


@dataclass(frozen=True, slots=True)
class ResolvedImageArchiveSettings:
    storage: S3ObjectStoreSettings
    prefix: str
    presign_seconds: int

    @property
    def bucket(self) -> str:
        return self.storage.bucket


class ImageArchiveSettings(BaseSettings):
    """Image archive policy layered over the primary object store."""

    bucket: str = ""
    prefix: str = ""
    presign_seconds: int = Field(default=DEFAULT_IMAGE_ARCHIVE_PRESIGN_SECONDS, gt=0)
    presigned_endpoint_url: str | None = None
    backend: ImageArchiveBackendSettings | None = None

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_IMAGE_ARCHIVE_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @field_validator("bucket", "prefix")
    @classmethod
    def normalize_path_component(cls, value: str) -> str:
        return value.strip().strip("/")

    @model_validator(mode="after")
    def validate_storage_selection(self) -> ImageArchiveSettings:
        if self.backend is not None and (self.bucket or self.presigned_endpoint_url is not None):
            raise ValueError(
                "image archive primary-store overrides cannot be set with a separate backend"
            )
        return self

    def resolve(
        self,
        primary: S3ObjectStoreSettings,
    ) -> ResolvedImageArchiveSettings:
        if self.backend is not None:
            storage = self.backend.object_store_settings()
        elif self.bucket or self.presigned_endpoint_url is not None:
            update: dict[str, str | None] = {}
            if self.bucket:
                update["bucket"] = self.bucket
            if self.presigned_endpoint_url is not None:
                update["presigned_endpoint_url"] = self.presigned_endpoint_url
            storage = primary.model_copy(update=update)
        else:
            storage = primary.model_copy()
        return ResolvedImageArchiveSettings(
            storage=storage,
            prefix=self.prefix,
            presign_seconds=self.presign_seconds,
        )
