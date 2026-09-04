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
    User = "user"
    Session = "session"
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

USER_PRINCIPAL_TOKEN_KINDS: frozenset[TokenKind] = frozenset(
    {
        TokenKind.Admin,
        TokenKind.User,
        TokenKind.Session,
    }
)
"""Kinds that name a person; their workspace reach is the account's memberships.

Everything else names one workspace and reaches only that workspace. Keeping both
is deliberate: an account-wide credential that leaked would reach production as
readily as a scratch workspace, so automation keeps a token whose blast radius is
a single workspace.
"""


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


class UserStatus(StringEnum):
    Active = "active"
    Disabled = "disabled"


class PlatformRole(StringEnum):
    """Platform-wide standing, independent of any one workspace."""

    Administrator = "administrator"
    Member = "member"


class WorkspaceRole(StringEnum):
    """A user's standing inside one workspace, ordered least to most authority."""

    Member = "member"
    Administrator = "administrator"
    Owner = "owner"


_WORKSPACE_ROLE_RANK: dict[WorkspaceRole, int] = {
    WorkspaceRole.Member: 0,
    WorkspaceRole.Administrator: 1,
    WorkspaceRole.Owner: 2,
}


def workspace_role_covers(held: WorkspaceRole, required: WorkspaceRole) -> bool:
    return _WORKSPACE_ROLE_RANK[held] >= _WORKSPACE_ROLE_RANK[required]


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


class IdentityProvider(StringEnum):
    Github = "github"


class UserRecord(ContractModel):
    """A person, plus the profile the provider last told us about them.

    An account with no `UserIdentityRecord` cannot sign in and exists to own
    tokens: the offline administrator and any automation account are this.
    """

    id: str
    display_name: str = ""
    email: str = ""
    avatar_url: str = ""
    role: PlatformRole = PlatformRole.Member
    status: UserStatus = UserStatus.Active
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserIdentityRecord(ContractModel):
    """The external account a person proves they control in order to sign in."""

    id: str
    user_id: str
    provider: IdentityProvider = IdentityProvider.Github
    subject: str = ""
    subject_login: str = ""
    # Null means the link was made somewhere with no provider to ask — the offline
    # bootstrap. Whatever reads this as an abuse signal must not treat that as new.
    provider_account_created_at: datetime | None = None
    last_authenticated_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkspaceMemberRecord(ContractModel):
    id: str
    workspace_id: str
    user_id: str
    role: WorkspaceRole = WorkspaceRole.Member
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkspaceInvitationRole(StringEnum):
    """The roles an invitation can offer. Owner is transferred, never offered."""

    Member = "member"
    Administrator = "administrator"

    @property
    def workspace_role(self) -> WorkspaceRole:
        return WorkspaceRole(self.value)


def fold_email(value: str) -> str:
    """The one spelling an address is compared under, on both sides of the comparison.

    Case-folded because the address on an invitation and the address the provider
    reports for the person who signs in are typed by different people, and a
    comparison that cared about case would refuse the person it was sent to.
    """
    return value.strip().lower()


def normalize_invitation_email(value: str) -> str:
    """A stored invitation address: folded, and shaped like a single mailbox."""
    email = fold_email(value)
    if len(email) > 320 or email.count("@") != 1 or any(c.isspace() for c in email):
        raise ValueError("invitation email must be a single address such as name@example.com")
    local, _, domain = email.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("invitation email must be a single address such as name@example.com")
    return email


class WorkspaceInvitationRecord(ContractModel):
    """An offer of membership that has not been answered yet.

    Only open offers exist. Accepting, declining and revoking all remove the row,
    because what became of an offer is a fact about the past and the audit
    history is what keeps it. What is left is what somebody can still act on.

    The address is where the offer was sent, and nothing else: whoever holds the
    link accepts, and the membership binds to the account they are signed in as.
    An address can move between people and an account can change the one it
    reports, so an offer keyed on it would follow the address rather than the
    person it was written for.
    """

    id: str
    workspace_id: str
    email: str
    role: WorkspaceInvitationRole = WorkspaceInvitationRole.Member
    invited_by_user_id: str = ""
    message_id: str = ""
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def expired_at(self, now: datetime) -> bool:
        """Whether this offer can still be answered, decided here and never by a caller.

        A browser asking its own clock would label an offer by how far that clock
        had drifted, and two people looking at one workspace would disagree about
        which invitations are live.
        """
        return self.expires_at <= now


class AuthTokenRecord(ContractModel):
    id: str
    name: str
    token_hash: str
    prefix: str
    kind: TokenKind = TokenKind.Workspace
    # Exactly one of these names the token's principal, which the schema enforces.
    # Empty rather than None because a UUID column round-trips "" as NULL here, and
    # every existing reader already treats an absent scope id as empty.
    user_id: str = ""
    workspace_id: str = ""
    worker_id: str = ""
    status: TokenStatus = TokenStatus.Active
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    reusable: bool = True
    disabled_by_admin: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def names_user(self) -> bool:
        return self.kind in USER_PRINCIPAL_TOKEN_KINDS


__all__ = [
    "SYSTEM_TOKEN_KINDS",
    "USER_PRINCIPAL_TOKEN_KINDS",
    "AuthScope",
    "AuthTokenRecord",
    "ConcurrencyLimitRecord",
    "DeviceAuthorizationStatus",
    "IdentityProvider",
    "PlatformRole",
    "TokenKind",
    "TokenStatus",
    "UserIdentityRecord",
    "UserRecord",
    "UserStatus",
    "WorkspaceInvitationRecord",
    "WorkspaceInvitationRole",
    "WorkspaceMemberRecord",
    "WorkspaceRecord",
    "WorkspaceRole",
    "WorkspaceStatus",
    "WorkspaceStorageConfig",
    "fold_email",
    "normalize_invitation_email",
    "workspace_role_covers",
]
