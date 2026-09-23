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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable, utc_now, uuid_type

_VOLUME_STATES = (
    "'none', 'creating', 'attaching', 'attached', 'releasing', 'detaching', 'cached', 'deleting'"
)
_VOLUME_SWEPT_STATES = (
    "'creating', 'attaching', 'attached', 'releasing', 'detaching', 'cached', 'deleting'"
)


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
        Index(
            "ix_disks_volume_due",
            "volume_changed_at",
            "id",
            postgresql_where=text(f"volume_state IN ({_VOLUME_SWEPT_STATES})"),
        ),
        Index(
            "ix_disks_volume_connection",
            "volume_connection_id",
            postgresql_where=text("volume_connection_id IS NOT NULL"),
        ),
        CheckConstraint(f"volume_state IN ({_VOLUME_STATES})", name="ck_disks_volume_state"),
        CheckConstraint(
            "volume_state = 'none' OR (volume_provider_ref <> '' AND volume_region <> '' "
            "AND volume_zone <> '' AND volume_size_bytes > 0 AND volume_token <> '' "
            "AND volume_capacity_workspace_id <> '' AND volume_driver <> '' "
            "AND volume_changed_at IS NOT NULL)",
            name="ck_disks_volume_scope",
        ),
        CheckConstraint(
            "volume_state IN ('none', 'creating') OR volume_id <> ''",
            name="ck_disks_volume_id",
        ),
        CheckConstraint(
            "volume_state NOT IN ('creating', 'attaching', 'attached', 'releasing', 'detaching') "
            "OR volume_instance_id <> ''",
            name="ck_disks_volume_instance",
        ),
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

    volume_state: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    """Where the disk's provider volume is; see `storage.disk_volumes`."""

    volume_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    volume_provider_ref: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    """The pooled provider whose account holds the volume. Kept after the volume is
    gone, so the orphan sweep still knows where this disk's volumes were made."""

    volume_connection_id: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    """The connected account holding the volume, while one exists there."""

    volume_capacity_workspace_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """The workspace the machine's capacity belongs to, which resolves the provider account."""

    volume_region: Mapped[str] = mapped_column(Text, nullable=False, default="")
    volume_zone: Mapped[str] = mapped_column(Text, nullable=False, default="")
    volume_instance_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    """The machine the volume is attached to, being attached to, or leaving."""

    volume_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    volume_token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    """Names the creation that made the volume, so a retried create finds it."""

    volume_formatted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """A lease has held the volume attached and let it go, so it carries a filesystem."""

    volume_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    """Advances with every volume transition; each one is conditional on the one it read."""

    volume_driver: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    """Who moved the volume into its current state, and so who may make its provider call."""

    volume_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DiskAttachmentTable(IdTable, DatabaseBase):
    """One lease on a disk, from acquisition to release, and how much of it is billed.

    A row per lease rather than a timestamp on the disk: the size can grow between
    leases, and each lease is billed at the size it was acquired at. A lease that
    ends is closed here and priced up to that instant by the next metering pass,
    whatever acquires the disk after it.
    """

    __tablename__ = "disk_attachments"
    __table_args__: tuple[SchemaItem, ...] = (
        Index(
            "uq_disk_attachments_open",
            "disk_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index(
            "ix_disk_attachments_unsettled",
            "metered_at",
            "id",
            postgresql_where=text("settled_at IS NULL"),
        ),
        CheckConstraint("size_bytes > 0", name="ck_disk_attachments_size_positive"),
        CheckConstraint(
            "released_at IS NULL OR released_at >= acquired_at",
            name="ck_disk_attachments_release_after_acquire",
        ),
        CheckConstraint(
            "metered_at >= acquired_at", name="ck_disk_attachments_metered_after_acquire"
        ),
        CheckConstraint(
            "settled_at IS NULL OR released_at IS NOT NULL",
            name="ck_disk_attachments_settled_released",
        ),
    )

    disk_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("disks.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    container_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """The disk's declared size when this lease was acquired, which is what it bills."""

    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """Where the next metering window starts; the lease is billed up to here."""

    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Set once a released lease is billed to its release, taking it out of every scan."""


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
