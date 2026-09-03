from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator
from shared.contracts import ContractModel
from shared.image_building.credentials import normalize_registry_host, registry_host_for_image


class CacheOriginCredentialRequest(ContractModel):
    workspace_id: str
    container_id: str
    stub_id: str = ""
    image_id: str = ""


class ImageRegistryCredentials(ContractModel):
    registry: str
    username: str = Field(default="", repr=False)
    password: str = Field(default="", repr=False)
    auth: str = Field(default="", repr=False)
    identity_token: str = Field(default="", repr=False)
    registry_token: str = Field(default="", repr=False)
    expires_at: datetime | None = None

    @field_validator("registry")
    @classmethod
    def normalize_registry(cls, value: str) -> str:
        normalized = normalize_registry_host(value)
        if not normalized:
            raise ValueError("image registry credentials require a valid registry")
        return normalized

    @model_validator(mode="after")
    def require_complete_authentication(self) -> ImageRegistryCredentials:
        basic = bool(self.username or self.password)
        if basic and not (self.username and self.password):
            raise ValueError("image registry basic credentials require username and password")
        methods = sum(
            bool(value) for value in (basic, self.auth, self.identity_token, self.registry_token)
        )
        if methods > 1:
            raise ValueError("image registry credentials require at most one authentication method")
        return self


class ImageArchiveUploadCredentialRequest(ContractModel):
    workspace_id: str
    stub_id: str = ""
    build_id: str
    container_id: str
    image_id: str = ""
    upload_capability: str = Field(min_length=32, max_length=64, pattern=r"^[0-9a-f]+$")
    archive_size_bytes: int = Field(gt=0)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_ref: str = Field(min_length=1)
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    architecture: str = Field(pattern=r"^(?:amd64|arm64)$")
    format_version: int = Field(default=2, ge=2)
    content_type: str = "application/vnd.lazycloud.image-index"

    @model_validator(mode="after")
    def registry_ref_names_manifest(self) -> ImageArchiveUploadCredentialRequest:
        if not self.registry_ref.endswith(f"@{self.manifest_digest}"):
            raise ValueError("image registry reference must name the manifest digest")
        return self


class CacheOriginCredentials(ContractModel):
    ok: bool = True
    error_msg: str = ""
    image_archive_url: str = Field(default="", repr=False)
    archive_size_bytes: int = Field(default=0, ge=0)
    archive_sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    registry_repository: str = ""
    registry_ref: str = ""
    manifest_digest: str = Field(default="", pattern=r"^(?:sha256:[0-9a-f]{64})?$")
    architecture: str = Field(default="", pattern=r"^(?:amd64|arm64)?$")
    format_version: int = Field(default=0, ge=0)
    registry_credentials: ImageRegistryCredentials | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def require_download_identity(self) -> CacheOriginCredentials:
        if self.image_archive_url and (self.archive_size_bytes <= 0 or not self.archive_sha256):
            raise ValueError("image archive URL requires its durable archive identity")
        if self.image_archive_url and (
            not self.registry_ref
            or not self.manifest_digest
            or not self.architecture
            or self.format_version < 2
        ):
            raise ValueError("image index URL requires its immutable OCI descriptor")
        if self.registry_ref and not self.registry_ref.endswith(f"@{self.manifest_digest}"):
            raise ValueError("image registry reference must name the manifest digest")
        if (
            self.registry_ref
            and self.registry_credentials is not None
            and (self.registry_credentials.registry != registry_host_for_image(self.registry_ref))
        ):
            raise ValueError("image registry credentials do not match the image descriptor")
        return self

    @classmethod
    def denied(cls, message: str) -> CacheOriginCredentials:
        return cls(ok=False, error_msg=message)


class ImageArchiveUploadCredentials(ContractModel):
    """Where an archive belongs, and whether this build still has to write it.

    A missing ``upload_url`` with a populated key is the dedup path: another build
    already published these exact bytes, so the worker verifies and skips the PUT.
    """

    ok: bool = True
    error_msg: str = ""
    bucket: str = ""
    object_key: str = ""
    archive_size_bytes: int = Field(default=0, ge=0)
    archive_sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    upload_url: str = Field(default="", repr=False)
    upload_headers: dict[str, str] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def require_upload_identity(self) -> ImageArchiveUploadCredentials:
        if bool(self.upload_url) != bool(self.upload_headers):
            raise ValueError("image archive upload URL and headers are inseparable")
        if (
            self.ok
            and self.object_key
            and (self.archive_size_bytes <= 0 or not self.archive_sha256)
        ):
            raise ValueError("image archive upload requires its durable archive identity")
        if self.upload_url and not self.object_key:
            raise ValueError("image archive upload URL requires its object key")
        return self

    @classmethod
    def denied(cls, message: str) -> ImageArchiveUploadCredentials:
        return cls(ok=False, error_msg=message)
