from __future__ import annotations

from datetime import datetime

from shared.app_identity import NAME
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import (
    DatabaseBase,
    IdPayloadTable,
    IdTable,
    NamedWorkspacePayloadTable,
    TimestampMixin,
    json_type,
    uuid_type,
)


class IdentityBootstrapClaimTable(TimestampMixin, DatabaseBase):
    """Singleton PostgreSQL claim that permanently closes initial bootstrap."""

    __tablename__ = "identity_bootstrap_claims"

    claim_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    admin_token_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tokens.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdentityAdminRecoveryRequestTable(TimestampMixin, DatabaseBase):
    """Auditable idempotency record for one offline administrator recovery."""

    __tablename__ = "identity_admin_recovery_requests"

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    admin_token_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tokens.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "workspaces"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("name", name="uq_workspaces_name"),
        UniqueConstraint("external_id", name="uq_workspaces_external_id"),
        Index("ix_workspaces_external_id", "external_id"),
    )

    external_id: Mapped[str] = mapped_column(
        uuid_type,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    signing_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    storage_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspace_storage.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    volume_cache_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    multi_gpu_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WorkspaceStorageTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "workspace_storage"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("workspace_id", name="uq_workspace_storage_workspace"),
        Index("ix_workspace_storage_workspace", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    bucket_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    endpoint_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    region: Mapped[str] = mapped_column(String(128), nullable=False, default="")


class WorkspaceAuditEventTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "workspace_audit_events"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_workspace_audit_workspace_created", "workspace_id", "created_at", "id"),
        Index("ix_workspace_audit_actor", "actor_token_id", "created_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_token_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(160), nullable=False)


class TokenTable(IdTable, DatabaseBase):
    __tablename__ = "tokens"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("token_hash", name="uq_tokens_token_hash"),
        Index("ix_tokens_workspace_created", "workspace_id", "created_at", "id"),
        Index("ix_tokens_prefix", "prefix"),
        CheckConstraint(
            "consumed_at IS NULL OR reusable = false",
            name="ck_tokens_consumed_non_reusable",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR (status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_tokens_consumed_terminal",
        ),
    )

    name: Mapped[str] = mapped_column(String(240), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker_id: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(json_type, nullable=False, default=lambda: ["*"])
    reusable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    disabled_by_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceAuthorizationTable(IdTable, DatabaseBase):
    __tablename__ = "device_authorizations"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("device_code_hash", name="uq_device_authorizations_device_code_hash"),
        UniqueConstraint("user_code", name="uq_device_authorizations_user_code"),
        Index("ix_device_authorizations_expires_at", "expires_at"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied')",
            name="ck_device_authorizations_status",
        ),
        CheckConstraint(
            "(status = 'approved' AND workspace_id IS NOT NULL) OR "
            "(status IN ('pending', 'denied') AND workspace_id IS NULL)",
            name="ck_device_authorizations_workspace_state",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR status IN ('approved', 'denied')",
            name="ck_device_authorizations_consumed_terminal",
        ),
    )

    device_code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    user_code: Mapped[str] = mapped_column(String(32), nullable=False)
    client_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConcurrencyLimitTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "concurrency_limits"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_concurrency_limits_workspace_name_created", "workspace_id", "name", "created_at"),
        CheckConstraint('"limit" > 0', name="ck_concurrency_limits_limit_positive"),
        CheckConstraint("in_flight >= 0", name="ck_concurrency_limits_in_flight_nonnegative"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False, default=NAME)
    limit: Mapped[int] = mapped_column(Integer, nullable=False)
    in_flight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(120), nullable=False, default=NAME)
    resource_id: Mapped[str | None] = mapped_column(String(160), nullable=True)


class SecretTable(IdTable, DatabaseBase):
    __tablename__ = "workspace_secrets"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("workspace_id", "name", name="uq_workspace_secrets_workspace_name"),
        Index("ix_workspace_secrets_workspace", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)


class CredentialTable(NamedWorkspacePayloadTable, DatabaseBase):
    __tablename__ = "credentials"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("workspace_id", "name", name="uq_credentials_workspace_name"),
        Index("ix_credentials_workspace", "workspace_id"),
    )

    token_prefix: Mapped[str] = mapped_column(String(80), nullable=False)
    labels_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
