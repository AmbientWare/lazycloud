from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from database.tables.base import DatabaseBase, uuid_type


class CapacityActivationTable(DatabaseBase):
    __tablename__ = "capacity_activations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('provision', 'boot', 'resume')", name="ck_capacity_activation_kind"
        ),
        CheckConstraint(
            "sleep_outcome IN ('unknown', 'stopped', 'hibernated')",
            name="ck_capacity_activation_sleep_outcome",
        ),
        CheckConstraint(
            "failed_at IS NULL OR (ready_at IS NULL AND prepared_at IS NULL)",
            name="ck_capacity_activation_terminal",
        ),
        Index("ix_capacity_activations_requested", "requested_at"),
        Index(
            "uq_capacity_activation_pending",
            "instance_record_id",
            unique=True,
            postgresql_where=text("ready_at IS NULL AND prepared_at IS NULL AND failed_at IS NULL"),
        ),
    )

    instance_record_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("compute_provider_instances.id", ondelete="CASCADE"),
        primary_key=True,
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    sleep_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    provider_running_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
