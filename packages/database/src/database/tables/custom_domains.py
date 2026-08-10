from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdPayloadTable, uuid_type


class CustomDomainTable(IdPayloadTable, DatabaseBase):
    """A domain a user registered, and the provider hostname backing it.

    Owned by the account rather than a workspace because DNS control was proven once
    by whoever owns the domain, and proving it again per workspace asks the same
    question the same answer. Which deployment serves the hostname stays a separate
    decision, made at deploy time in whichever of the owner's workspaces it lives.
    """

    __tablename__ = "custom_domains"
    __table_args__: tuple[SchemaItem, ...] = (
        # Global rather than per owner: a hostname resolves at the public edge with no
        # tenant context, so two accounts claiming one name would be two answers to
        # the same question.
        Index(
            "uq_custom_domains_hostname_active",
            "hostname",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_custom_domains_user", "user_id", "hostname"),
        Index("ix_custom_domains_reconcile_due", "phase", "last_checked_at"),
        CheckConstraint(
            "phase IN ('awaiting_verification', 'validating', 'ready', 'action_required')",
            name="ck_custom_domains_phase",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_hostname_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["CustomDomainTable"]
