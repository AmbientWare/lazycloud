from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdPayloadTable, uuid_type


class AppTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "apps"
    __table_args__: tuple[SchemaItem, ...] = (
        Index(
            "uq_apps_workspace_name_active",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_apps_workspace_updated", "workspace_id", "updated_at", "id"),
        Index("ix_apps_name", "name"),
        Index(
            "ix_apps_lifecycle_reconcile",
            "lifecycle_state",
            "reconcile_claimed_at",
            "updated_at",
        ),
        CheckConstraint("lifecycle_revision >= 0", name="ck_apps_lifecycle_revision"),
        CheckConstraint(
            "reconcile_attempt_count >= 0",
            name="ck_apps_reconcile_attempt_count",
        ),
        CheckConstraint(
            "(lifecycle_state = 'deleted') = (deleted_at IS NOT NULL)",
            name="ck_apps_deleted_state_timestamp",
        ),
        CheckConstraint(
            "lifecycle_state IN ('active', 'paused', 'deleted', 'pausing', 'resuming', "
            "'deleting', 'cleanup_failed')",
            name="ck_apps_lifecycle_state",
        ),
        CheckConstraint(
            "lifecycle_target IS NULL OR lifecycle_target IN ('active', 'paused', 'deleted')",
            name="ck_apps_lifecycle_target",
        ),
        CheckConstraint(
            "(lifecycle_state IN ('pausing', 'resuming', 'deleting', 'cleanup_failed')) "
            "= (lifecycle_target IS NOT NULL AND lifecycle_operation_id IS NOT NULL)",
            name="ck_apps_unfinished_operation",
        ),
        CheckConstraint(
            "(lifecycle_state <> 'pausing' OR lifecycle_target = 'paused') AND "
            "(lifecycle_state <> 'resuming' OR lifecycle_target = 'active') AND "
            "(lifecycle_state <> 'deleting' OR lifecycle_target = 'deleted')",
            name="ck_apps_lifecycle_transition",
        ),
        CheckConstraint(
            "(lifecycle_event_id IS NULL) = (lifecycle_event_created_at IS NULL) "
            "AND (lifecycle_change_published_at IS NULL OR lifecycle_event_id IS NOT NULL)",
            name="ck_apps_lifecycle_publication",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    stub_id: Mapped[str | None] = mapped_column(
        uuid_type,
        # `use_alter` because apps and stubs point at each other, and a schema
        # with a cycle in it has no order that creates both tables with their
        # keys inline. This edge is the one broken out because it is a nullable
        # pointer at the current stub rather than the stub's own ownership.
        ForeignKey("stubs.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    lifecycle_revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifecycle_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lifecycle_operation_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    lifecycle_failure: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reconcile_claim_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    reconcile_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconcile_attempt_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifecycle_event_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    lifecycle_event_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lifecycle_change_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AppDeploymentIntentTable(DatabaseBase):
    __tablename__ = "app_deployment_intents"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "operation_revision >= 0",
            name="ck_app_deployment_intents_revision",
        ),
        CheckConstraint(
            "target IN ('active', 'inactive', 'deleted')",
            name="ck_app_deployment_intents_target",
        ),
        CheckConstraint(
            "(event_id IS NULL) = (event_created_at IS NULL) "
            "AND (workspace_change_published_at IS NULL OR event_id IS NOT NULL)",
            name="ck_app_deployment_intents_publication",
        ),
        Index("ix_app_deployment_intents_deployment", "deployment_id"),
    )

    app_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("apps.id", ondelete="CASCADE"),
        primary_key=True,
    )
    deployment_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("deployments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    operation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    event_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    event_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    workspace_change_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class AppContainerShutdownIntentTable(DatabaseBase):
    __tablename__ = "app_container_shutdown_intents"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "operation_revision >= 0",
            name="ck_app_container_shutdown_intents_revision",
        ),
        Index("ix_app_container_shutdown_intents_container", "container_id"),
    )

    app_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("apps.id", ondelete="CASCADE"),
        primary_key=True,
    )
    container_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("containers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    worker_id: Mapped[str] = mapped_column(String(240), nullable=False)
    operation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class StubTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "stubs"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("external_id", name="uq_stubs_external_id"),
        UniqueConstraint(
            "workspace_id", "preparation_fingerprint", name="uq_stubs_preparation_fingerprint"
        ),
        Index("ix_stubs_workspace", "workspace_id"),
        Index(
            "ix_stubs_reusable_identity",
            "workspace_id",
            "name",
            "app_id",
            "created_at",
            "id",
            postgresql_where=text("CAST((payload ->> 'deployment_id') AS VARCHAR) IS NULL"),
            sqlite_where=text("JSON_EXTRACT(payload, '$.\"deployment_id\"') IS NULL"),
        ),
        Index("ix_stubs_app_created", "app_id", "created_at", "id"),
        Index("ix_stubs_app_type_created", "app_id", "type", "created_at", "id"),
        Index("ix_stubs_payload_artifact_refs", "payload", postgresql_using="gin"),
        Index("ix_stubs_group", "group"),
    )

    external_id: Mapped[str] = mapped_column(
        uuid_type,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    object_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("objects.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    group: Mapped[str | None] = mapped_column(String(240), nullable=True)
    public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    preparation_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DeploymentTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "deployments"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "workspace_id",
            "app_id",
            "name",
            "version",
            "kind",
            name="uq_deployments_workspace_app_name_version_kind",
        ),
        CheckConstraint("version >= 0", name="ck_deployments_version_nonnegative"),
        Index("ix_deployments_workspace_created", "workspace_id", "created_at", "id"),
        Index("ix_deployments_app_created", "app_id", "created_at", "id"),
        Index("ix_deployments_stub", "stub_id"),
        # The public edge routes on this pair alone, so a digest collision between two
        # resources must fail the second deploy rather than silently answer for it.
        Index(
            "uq_deployments_subdomain_version_active",
            "subdomain",
            "version",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        # Same reasoning for a claimed hostname. Nulls do not collide, so resources
        # that claimed nothing are not treated as claiming the same thing.
        Index(
            "uq_deployments_custom_hostname_version_active",
            "custom_hostname",
            "version",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    stub_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("stubs.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    subdomain: Mapped[str] = mapped_column(String(63), nullable=False)
    custom_hostname: Mapped[str | None] = mapped_column(String(253), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CronJobTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "cron_jobs"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_cron_jobs_deployment", "deployment_id"),
        Index("ix_cron_jobs_workspace", "workspace_id"),
        Index(
            "ix_cron_jobs_due",
            "next_run_at",
            "id",
            postgresql_where=text("enabled IS TRUE"),
            sqlite_where=text("enabled = 1"),
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    deployment_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    cron: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
