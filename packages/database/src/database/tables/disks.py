from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable, utc_now, uuid_type


class DiskTable(IdTable, DatabaseBase):
    __tablename__ = "disks"
    __table_args__: tuple[SchemaItem, ...] = (
        Index(
            "uq_disks_workspace_name_live",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_disks_metered_at_live",
            "metered_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_disks_deleting",
            "deleted_at",
            "id",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
        Index("ix_disks_holder", "holder_container_id"),
        CheckConstraint("size_bytes > 0", name="ck_disks_size_positive"),
        CheckConstraint("generation >= 0", name="ck_disks_generation_nonnegative"),
        CheckConstraint("stored_bytes >= 0", name="ck_disks_stored_bytes_nonnegative"),
        CheckConstraint("metered_bytes >= 0", name="ck_disks_metered_bytes_nonnegative"),
        CheckConstraint(
            "(holder_container_id IS NULL) = (lease_token = '')",
            name="ck_disks_holder_has_lease",
        ),
        CheckConstraint(
            "(status = 'deleting' AND deleted_at IS NOT NULL AND holder_container_id IS NULL) "
            "OR (status = 'attached' AND deleted_at IS NULL AND holder_container_id IS NOT NULL) "
            "OR (status = 'detached' AND deleted_at IS NULL AND holder_container_id IS NULL)",
            name="ck_disks_status",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="detached")
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    holder_container_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    """The container writing the disk. No foreign key: a vanished row is a released holder."""

    lease_token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    last_worker_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    metered_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    """Bytes stored when the last metering window closed; what the next one bills."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When deletion was requested. The name is free and metering ends from here."""


class DiskGenerationTable(DatabaseBase):
    __tablename__ = "disk_generations"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("generation > 0", name="ck_disk_generations_generation_positive"),
        CheckConstraint(
            "parent_generation >= 0 AND parent_generation < generation",
            name="ck_disk_generations_parent_before",
        ),
        CheckConstraint(
            "stored_bytes_added >= 0", name="ck_disk_generations_stored_bytes_nonnegative"
        ),
    )

    disk_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("disks.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    parent_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest_key: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    stored_bytes_added: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
