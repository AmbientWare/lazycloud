from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import (
    DatabaseBase,
    IdPayloadTable,
    IdTable,
    NamedWorkspacePayloadTable,
    utc_now,
    uuid_type,
)


class ObjectTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "objects"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("workspace_id", "bucket", "key", name="uq_objects_workspace_bucket_key"),
        Index("ix_objects_workspace_key", "workspace_id", "key"),
        Index("ix_objects_workspace_sha256", "workspace_id", "sha256"),
        Index("ix_objects_write_claimed_at", "write_claimed_at"),
        Index("ix_objects_cleanup_claimed_at", "cleanup_claimed_at"),
        CheckConstraint("size >= 0", name="ck_objects_size_nonnegative"),
        Index(
            "ix_objects_artifact_listing", "workspace_id", "artifact_task_id", "created_at", "id"
        ),
        Index("ix_objects_artifact_app", "workspace_id", "artifact_app_id"),
        Index("ix_objects_artifact_expiration", "artifact_expires_at", "id"),
        Index("ix_objects_artifact_metering", "artifact_metered_at", "id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(1024), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    write_claim_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    write_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cleanup_kind: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    cleanup_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    artifact_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    artifact_metered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ArtifactRetentionTable(DatabaseBase):
    __tablename__ = "artifact_retention"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("retention_seconds > 0", name="ck_artifact_retention_positive"),
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    retention_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class VolumeTable(NamedWorkspacePayloadTable, DatabaseBase):
    __tablename__ = "volumes"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("workspace_id", "name", name="uq_volumes_workspace_name"),
        Index("ix_volumes_workspace", "workspace_id"),
        Index("ix_volumes_metered_at", "metered_at", "id"),
        Index("ix_volumes_deletion_requested_at", "deletion_requested_at", "id"),
        CheckConstraint("size_bytes >= 0", name="ck_volumes_size_bytes_nonnegative"),
    )

    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unfenced_writes_possible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class VolumeCleanupTable(DatabaseBase):
    __tablename__ = "volume_cleanup"
    __table_args__ = (Index("ix_volume_cleanup_swept_at", "swept_at", "volume_id"),)

    volume_id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    swept_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class CacheEntryTable(IdTable, DatabaseBase):
    __tablename__ = "cache_entries"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("key", name="uq_cache_entries_key"),
        CheckConstraint("size >= 0", name="ck_cache_entries_size_nonnegative"),
        CheckConstraint("hits >= 0", name="ck_cache_entries_hits_nonnegative"),
        CheckConstraint(
            "created_at <= updated_at",
            name="ck_cache_entries_timestamp_order",
        ),
    )

    key: Mapped[str] = mapped_column(String(512), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    hits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
