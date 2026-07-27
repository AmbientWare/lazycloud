from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue, field_validator

from shared.app_identity import NAME
from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now


class TokenStatus(StringEnum):
    Active = "active"
    Revoked = "revoked"


class DeviceAuthorizationStatus(StringEnum):
    """Lifecycle of a device-code login request.

    ``Expired`` is derived for pollers; stored rows use pending, approved, or
    denied and are pruned after expiry.
    """

    Pending = "pending"
    Approved = "approved"
    Denied = "denied"
    Expired = "expired"


class TokenKind(StringEnum):
    Admin = "admin"
    WorkspacePrimary = "workspace-primary"
    Workspace = "workspace"
    WorkspaceRestricted = "workspace-restricted"
    Worker = "worker"
    WorkerPrivate = "worker-private"
    Machine = "machine"


SYSTEM_TOKEN_KINDS: frozenset[TokenKind] = frozenset(
    {
        TokenKind.WorkspaceRestricted,
        TokenKind.Worker,
        TokenKind.WorkerPrivate,
        TokenKind.Machine,
    }
)
"""Platform-minted token kinds eligible for expiry pruning and dashboard hiding."""


class AuthScope(StringEnum):
    Read = "read"
    Write = "write"
    Admin = "admin"
    Worker = "worker"
    Machine = "machine"


class WorkspaceStatus(StringEnum):
    Active = "active"
    Disabled = "disabled"
    Deleting = "deleting"
    Deleted = "deleted"


class WorkspaceStorageConfig(ContractModel):
    backend: str = "local"
    bucket: str | None = None
    prefix: str = ""
    config: dict[str, JsonValue] = Field(default_factory=dict)

    # The connection settings arrive as an open JSON bag because an externally
    # attached bucket may carry provider-specific keys. These accessors are the
    # one place that bag is read, so every consumer normalizes it identically.

    @property
    def endpoint_url(self) -> str:
        return _storage_config_text(self.config.get("endpoint_url"))

    @property
    def region(self) -> str:
        return _storage_config_text(self.config.get("region"))

    @property
    def access_key(self) -> str:
        return _storage_config_text(self.config.get("access_key"))

    @property
    def secret_key(self) -> str:
        return _storage_config_text(self.config.get("secret_key"))

    @property
    def force_path_style(self) -> bool:
        value = self.config.get("force_path_style")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"1", "true", "yes", "on"}
        return False

    @property
    def key_prefix(self) -> str:
        """Normalize `prefix` into a key-joinable form, empty or trailing-slashed.

        The container's mount and the presigned URL both derive their keys from
        this, so it is the one place the two can be kept in step.
        """
        segments = [segment for segment in self.prefix.split("/") if segment and segment != "."]
        return f"{'/'.join(segments)}/" if segments else ""


def _storage_config_text(value: JsonValue) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


class WorkspaceRecord(ContractModel):
    id: str
    name: str
    status: WorkspaceStatus = WorkspaceStatus.Active
    signing_key_prefix: str | None = None
    signing_key: str = ""
    primary_token_id: str | None = None
    concurrency_limit_id: str | None = None
    storage: WorkspaceStorageConfig = Field(default_factory=WorkspaceStorageConfig)
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConcurrencyLimitRecord(ContractModel):
    id: str
    workspace_id: str
    name: str
    limit: int
    in_flight: int = 0
    resource_type: str = NAME
    resource_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("limit")
    @classmethod
    def limit_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "limit must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("in_flight")
    @classmethod
    def in_flight_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "in_flight cannot be negative"
            raise ValueError(msg)
        return value

    @property
    def available(self) -> int:
        return max(self.limit - self.in_flight, 0)

    @property
    def saturated(self) -> bool:
        return self.available == 0


class AuthTokenRecord(ContractModel):
    id: str
    name: str
    token_hash: str
    prefix: str
    kind: TokenKind = TokenKind.Workspace
    workspace_id: str
    worker_id: str = ""
    status: TokenStatus = TokenStatus.Active
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    reusable: bool = True
    disabled_by_admin: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


__all__ = [
    "SYSTEM_TOKEN_KINDS",
    "AuthScope",
    "AuthTokenRecord",
    "ConcurrencyLimitRecord",
    "DeviceAuthorizationStatus",
    "TokenKind",
    "TokenStatus",
    "WorkspaceRecord",
    "WorkspaceStatus",
    "WorkspaceStorageConfig",
]
