from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from shared.contracts import ContractModel
from shared.identity import WorkspaceStorageConfig


class WorkspaceStorageGrant(ContractModel):
    """Credentials for one workspace's S3-compatible bucket."""

    endpoint_url: str = Field(min_length=1)
    region: str = ""
    bucket_name: str = ""
    prefix: str = ""
    force_path_style: bool = False
    access_key: str = Field(default="", repr=False)
    secret_key: str = Field(default="", repr=False)
    session_token: str = Field(default="", repr=False)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def expiration_is_absolute(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("workspace storage expiry must carry a timezone")
        return value

    @model_validator(mode="after")
    def credentials_are_usable(self) -> WorkspaceStorageGrant:
        if not self.bucket_name:
            raise ValueError("workspace storage grant requires a bucket")
        if bool(self.access_key) != bool(self.secret_key):
            raise ValueError("workspace storage grant requires both key and secret, or neither")
        if self.session_token and not self.access_key:
            raise ValueError("workspace storage session token requires an access key")
        return self


class WorkspaceStorageIssuer(Protocol):
    """The authority for workspace storage grants and their retirement."""

    def issue(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> WorkspaceStorageGrant:
        """Vend a credential for this workspace's bucket, and no other."""
        ...

    def retire(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> None:
        """Retire platform-owned storage after workspace writers have stopped."""
        ...


__all__ = [
    "WorkspaceStorageGrant",
    "WorkspaceStorageIssuer",
]
