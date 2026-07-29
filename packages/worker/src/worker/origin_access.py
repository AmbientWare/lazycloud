from __future__ import annotations

from pydantic import Field, model_validator
from shared.contracts import ContractModel


class CacheOriginCredentialRequest(ContractModel):
    workspace_id: str
    container_id: str
    stub_id: str = ""
    image_id: str = ""


class ImageArchiveUploadCredentialRequest(ContractModel):
    workspace_id: str
    stub_id: str = ""
    build_id: str
    container_id: str
    image_id: str = ""
    upload_capability: str = Field(min_length=32, max_length=64, pattern=r"^[0-9a-f]+$")
    archive_size_bytes: int = Field(gt=0)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = "application/x-tar"


class CacheOriginCredentials(ContractModel):
    ok: bool = True
    error_msg: str = ""
    image_archive_url: str = Field(default="", repr=False)
    archive_size_bytes: int = Field(default=0, ge=0)
    archive_sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")

    @model_validator(mode="after")
    def require_download_identity(self) -> CacheOriginCredentials:
        if self.image_archive_url and (self.archive_size_bytes <= 0 or not self.archive_sha256):
            raise ValueError("image archive URL requires its durable archive identity")
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
