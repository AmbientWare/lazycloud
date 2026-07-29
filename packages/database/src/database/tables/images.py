from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
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

from database.tables.base import DatabaseBase, IdPayloadTable, uuid_type


class ImageArchiveTable(IdPayloadTable, DatabaseBase):
    """The one archive for an image, owned by the system rather than a workspace.

    Image identity is already global — it digests the dockerfile, build context,
    architecture and secret versions, never a workspace — so the bytes it names are
    the same bytes for every tenant. Holding one archive per image is what makes the
    worker's image cache, which has always been keyed on the image id alone,
    consistent with durable state.

    Authorization is a join, not a namespace: a workspace reaches this row only
    through its own `images` row for the same image id.
    """

    __tablename__ = "image_archives"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("image_id", name="uq_image_archives_image_id"),
        Index("ix_image_archives_cleanup_claimed_at", "cleanup_claimed_at"),
        Index("ix_image_archives_updated_at", "updated_at"),
        CheckConstraint("size_bytes > 0", name="ck_image_archives_size_positive"),
        CheckConstraint("length(sha256) = 64", name="ck_image_archives_sha256_complete"),
        CheckConstraint("object_key <> ''", name="ck_image_archives_object_key_present"),
    )

    image_id: Mapped[str] = mapped_column(String(512), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cleanup_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ImageTable(IdPayloadTable, DatabaseBase):
    """A workspace's authorization to use an image, and its clip version.

    Archive facts live on `image_archives`. Keeping them here made every workspace
    carry its own digest of shared content, so deleting one tenant destroyed bytes a
    surviving tenant referenced, and the workspace-scoped `RESTRICT` foreign key onto
    `objects` wedged deletion after the bytes were already gone.
    """

    __tablename__ = "images"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "workspace_id",
            "image_id",
            name="uq_images_workspace_image_id",
        ),
        Index("ix_images_workspace", "workspace_id"),
        Index("ix_images_image_id", "image_id"),
        Index("ix_images_cleanup_claimed_at", "cleanup_claimed_at"),
        Index("ix_images_cleanup_completed_at", "cleanup_completed_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    image_id: Mapped[str] = mapped_column(String(512), nullable=False)
    clip_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    cleanup_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cleanup_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ImageBuildTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "image_builds"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_image_builds_workspace_created", "workspace_id", "created_at"),
        Index("ix_image_builds_status_created", "status", "created_at"),
        Index("ix_image_builds_image_id", "image_id"),
        Index(
            "ix_image_builds_workspace_image_created",
            "workspace_id",
            "image_id",
            "created_at",
        ),
        Index("ix_image_builds_cache_key", "cache_key"),
        Index(
            "ix_image_builds_workspace_fingerprint_status_created",
            "workspace_id",
            "fingerprint",
            "status",
            "created_at",
        ),
        Index("ix_image_builds_archive_path_digest", "archive_path_digest"),
        Index("ix_image_builds_manifest_path_digest", "manifest_path_digest"),
        Index("ix_image_builds_dockerfile_path_digest", "dockerfile_path_digest"),
        Index("ix_image_builds_cache_manifest_path_digest", "cache_manifest_path_digest"),
        Index("ix_image_builds_cache_publish_key", "cache_publish_key"),
        Index("ix_image_builds_cleanup_claimed_at", "cleanup_claimed_at"),
        Index(
            "uq_image_builds_active_workspace_fingerprint",
            "workspace_id",
            "fingerprint",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    image_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cache_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    archive_path_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    archive_path_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    manifest_path_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manifest_path_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    dockerfile_path_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dockerfile_path_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    cache_manifest_path_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cache_manifest_path_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    cache_publish_key: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    fingerprint: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    phase: Mapped[str] = mapped_column(String(80), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleanup_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    publication_claim_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    publication_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CheckpointTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "checkpoints"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("checkpoint_id", name="uq_checkpoints_checkpoint_id"),
        Index("ix_checkpoints_workspace_created", "workspace_id", "created_at"),
        Index("ix_checkpoints_stub_created", "stub_id", "created_at"),
        Index("ix_checkpoints_cache_hash", "cache_hash"),
        Index("ix_checkpoints_retention_expires_at", "retention_expires_at"),
        Index("ix_checkpoints_cleanup_claimed_at", "cleanup_claimed_at"),
        CheckConstraint("cache_size_bytes >= 0", name="ck_checkpoints_cache_size_nonnegative"),
    )

    checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_container_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("containers.id", ondelete="SET NULL"),
        nullable=True,
    )
    container_ip: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    remote_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    stub_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("stubs.id", ondelete="SET NULL"),
        nullable=True,
    )
    stub_type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    app_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
    )
    cache_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    cache_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    origin_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    locality: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    accelerator: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    last_restored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    retention_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cleanup_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
