from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from shared.contracts import ContractModel
from shared.identity import WorkspaceStorageConfig


class WorkspaceStorageIssuerKind(StrEnum):
    """Which store issues this deployment's workspace credentials.

    Named rather than inferred from whatever object-store settings happen to be
    present: the two stores need different grants, and a deployment that guessed
    would report a credential failure from inside a worker rather than a missing
    setting at startup.
    """

    Aws = "aws"
    Garage = "garage"


class WorkspaceStorageGrant(ContractModel):
    """Permission to reach one workspace's bucket, and nothing else.

    Every field is what an S3 client needs, because the three stores this has to
    serve all speak SigV4: AWS returns a session, Garage returns a key it granted
    on one bucket, and a connected account's own storage returns what the customer
    configured. Azure would need a different mount tool before it needed a
    different credential, so this stays S3-shaped until that is the actual problem.
    """

    endpoint_url: str = ""
    """Empty means the store's own public endpoint, which for AWS is real S3."""
    region: str = ""
    bucket_name: str = ""
    prefix: str = ""
    force_path_style: bool = False
    access_key: str = Field(default="", repr=False)
    secret_key: str = Field(default="", repr=False)
    session_token: str = Field(default="", repr=False)
    expires_at: datetime | None = None
    """When this stops working, or `None` for a store that cannot say.

    Absent is a fact about the store rather than about the deployment: GCS HMAC
    keys have no expiry to report. Both stores in use today set it, so the refresh
    that reads it runs everywhere rather than only in production.
    """

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
    """The authority that decides what a worker may reach in a workspace's bucket.

    Two calls because the two stores divide the work differently. AWS holds no
    per-workspace state and mints on demand; Garage creates a key once and rotates
    it. `provision` returns whatever the issuer needs stored on the workspace to
    answer `issue` later, which for AWS is nothing at all.
    """

    def provision(self, *, workspace_id: str, bucket: str) -> dict[str, str]:
        """Prepare durable state for a workspace's bucket, at creation."""
        ...

    def issue(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> WorkspaceStorageGrant:
        """Vend a credential for this workspace's bucket, and no other."""
        ...


__all__ = [
    "WorkspaceStorageGrant",
    "WorkspaceStorageIssuer",
    "WorkspaceStorageIssuerKind",
]
