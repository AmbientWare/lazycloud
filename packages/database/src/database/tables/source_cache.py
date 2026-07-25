from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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

from database.tables.base import DatabaseBase, IdTable, uuid_type


class WorkerCacheGenerationTable(IdTable, DatabaseBase):
    __tablename__ = "worker_cache_generations"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_worker_cache_generations_worker_state", "worker_id", "state"),
        Index("ix_worker_cache_generations_workspace_state", "workspace_id", "state"),
        Index("ix_worker_cache_generations_last_seen", "last_seen_at", "id"),
        Index(
            "uq_worker_cache_generations_active_storage",
            "storage_id",
            unique=True,
            postgresql_where=text("state <> 'retired'"),
            sqlite_where=text("state <> 'retired'"),
        ),
        CheckConstraint(
            "state IN ('initializing', 'available', 'draining', 'retired')",
            name="ck_worker_cache_generations_state",
        ),
        CheckConstraint(
            "session_fence > 0",
            name="ck_worker_cache_generations_session_fence_positive",
        ),
        CheckConstraint(
            "length(trim(worker_id)) > 0",
            name="ck_worker_cache_generations_worker_nonempty",
        ),
        CheckConstraint(
            "length(trim(storage_id)) > 0",
            name="ck_worker_cache_generations_storage_nonempty",
        ),
        CheckConstraint(
            "(state = 'retired' AND retired_at IS NOT NULL "
            "AND storage_destroyed_at IS NOT NULL) OR "
            "(state <> 'retired' AND retired_at IS NULL "
            "AND storage_destroyed_at IS NULL)",
            name="ck_worker_cache_generations_retirement",
        ),
    )

    worker_id: Mapped[str] = mapped_column(String(240), nullable=False)
    storage_id: Mapped[str] = mapped_column(String(512), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=True,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    session_fence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    storage_destroyed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SourceCacheCleanupTargetTable(IdTable, DatabaseBase):
    __tablename__ = "source_cache_cleanup_targets"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "workspace_id",
            "cache_generation_id",
            "source_object_id",
            name="uq_source_cache_cleanup_target",
        ),
        Index(
            "ix_source_cache_cleanup_targets_due",
            "cache_generation_id",
            "status",
            "next_attempt_at",
            "id",
        ),
        Index(
            "ix_source_cache_cleanup_targets_workspace",
            "workspace_id",
            "status",
        ),
        Index("ix_source_cache_cleanup_targets_claim_expiry", "claim_expires_at"),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'completed')",
            name="ck_source_cache_cleanup_targets_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_source_cache_cleanup_targets_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "claim_session_fence IS NULL OR claim_session_fence > 0",
            name="ck_source_cache_cleanup_targets_claim_fence_positive",
        ),
        CheckConstraint(
            "completion_reason IS NULL OR completion_reason IN ('purged', 'storage-destroyed')",
            name="ck_source_cache_cleanup_targets_completion_reason",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR last_error_code IN ('purge-failed')",
            name="ck_source_cache_cleanup_targets_error_code",
        ),
        CheckConstraint(
            "(status = 'pending' AND claim_token IS NULL "
            "AND claim_expires_at IS NULL AND claim_session_fence IS NULL "
            "AND completed_at IS NULL AND completion_reason IS NULL) OR "
            "(status = 'claimed' AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL AND claim_session_fence IS NOT NULL "
            "AND completed_at IS NULL AND completion_reason IS NULL) OR "
            "(status = 'completed' AND claim_token IS NULL "
            "AND claim_expires_at IS NULL AND claim_session_fence IS NULL "
            "AND completed_at IS NOT NULL AND completion_reason IS NOT NULL)",
            name="ck_source_cache_cleanup_targets_lifecycle",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cache_generation_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("worker_cache_generations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_object_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_session_fence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


__all__ = ["SourceCacheCleanupTargetTable", "WorkerCacheGenerationTable"]
