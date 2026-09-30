from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now

"""Platform-minted token kinds eligible for expiry pruning and dashboard hiding."""

"""Kinds that name a person; their workspace reach is the account's memberships.

Everything else names one workspace and reaches only that workspace. Keeping both
is deliberate: an account-wide credential that leaked would reach production as
readily as a scratch workspace, so automation keeps a token whose blast radius is
a single workspace.
"""


class WorkspaceKind(StringEnum):
    Tenant = "tenant"
    Platform = "platform"


class WorkspaceStatus(StringEnum):
    Active = "active"
    Disabled = "disabled"
    Deleting = "deleting"
    Deleted = "deleted"


class WorkspaceStorageConfig(ContractModel):
    """Where a workspace's bucket is. Credentials are never stored; an issuer vends them."""

    backend: str = "local"
    bucket: str | None = None
    prefix: str = ""
    endpoint_url: str = ""
    region: str = ""

    @property
    def key_prefix(self) -> str:
        """Normalize `prefix` into a key-joinable form, empty or trailing-slashed.

        The container's mount and the presigned URL both derive their keys from
        this, so it is the one place the two can be kept in step.
        """
        segments = [segment for segment in self.prefix.split("/") if segment and segment != "."]
        return f"{'/'.join(segments)}/" if segments else ""


class WorkspaceRecord(ContractModel):
    id: str
    name: str
    kind: WorkspaceKind = WorkspaceKind.Tenant
    status: WorkspaceStatus = WorkspaceStatus.Active
    signing_key_prefix: str | None = None
    signing_key: str = ""
    primary_token_id: str | None = None
    concurrency_limit_id: str | None = None
    connection_id: str | None = None
    """The connected cloud account this workspace lives in, or None for LazyCloud.

    Fixed at creation. Compute and the workspace bucket both follow it, so it is
    never updated; moving a workspace means creating another one.
    """
    storage: WorkspaceStorageConfig = Field(default_factory=WorkspaceStorageConfig)
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


__all__ = [
    "WorkspaceKind",
    "WorkspaceRecord",
    "WorkspaceStatus",
    "WorkspaceStorageConfig",
]
