from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.tables.base import DatabaseBase, uuid_type


class CapacityActivationTable(DatabaseBase):
    __tablename__ = "capacity_activations"
    __table_args__ = (
        UniqueConstraint(
            "instance_record_id", "requested_at", name="uq_capacity_activation_request"
        ),
        Index(
            "uq_capacity_activation_external_sleep",
            "instance_record_id",
            "sleep_attempt_id",
            unique=True,
            postgresql_where=text("requested_at IS NULL"),
        ),
        CheckConstraint(
            "requested_at IS NOT NULL OR sleep_attempt_id IS NOT NULL",
            name="ck_capacity_activation_external_sleep",
        ),
        CheckConstraint(
            "kind IN ('provision', 'boot', 'resume')", name="ck_capacity_activation_kind"
        ),
        CheckConstraint(
            "restore_outcome IN ('unknown', 'cold_boot', 'memory_restored')",
            name="ck_capacity_activation_restore_outcome",
        ),
        ForeignKeyConstraint(
            ["instance_record_id", "sleep_attempt_id"],
            ["capacity_sleep_attempts.instance_record_id", "capacity_sleep_attempts.id"],
            name="fk_capacity_activation_sleep",
        ),
        CheckConstraint(
            "failed_at IS NULL OR (ready_at IS NULL AND prepared_at IS NULL)",
            name="ck_capacity_activation_terminal",
        ),
        Index("ix_capacity_activations_requested", "requested_at"),
        Index(
            "ix_capacity_activations_sleep",
            "sleep_attempt_id",
            postgresql_where=text("sleep_attempt_id IS NOT NULL"),
        ),
        Index(
            "uq_capacity_activation_pending",
            "instance_record_id",
            unique=True,
            postgresql_where=text("ready_at IS NULL AND prepared_at IS NULL AND failed_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        uuid_type, primary_key=True, server_default=text("gen_random_uuid()")
    )
    instance_record_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("compute_provider_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    sleep_attempt_id: Mapped[str | None] = mapped_column(uuid_type)
    restore_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    restore_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_running_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
