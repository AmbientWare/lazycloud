from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.tables.base import DatabaseBase, uuid_type


class CapacitySleepAttemptTable(DatabaseBase):
    __tablename__ = "capacity_sleep_attempts"
    __table_args__ = (
        UniqueConstraint("instance_record_id", "id", name="uq_capacity_sleep_instance_attempt"),
        CheckConstraint(
            "requested_mode IS NULL OR requested_mode IN ('stop', 'hibernate')",
            name="ck_capacity_sleep_requested_mode",
        ),
        CheckConstraint(
            "accepted_mode IS NULL OR accepted_mode IN ('stop', 'hibernate')",
            name="ck_capacity_sleep_accepted_mode",
        ),
        CheckConstraint(
            "(accepted_mode IS NULL) = (accepted_at IS NULL)",
            name="ck_capacity_sleep_acceptance",
        ),
        CheckConstraint(
            "superseded_at IS NULL OR (superseded_at >= observed_at "
            "AND (accepted_at IS NULL OR superseded_at >= accepted_at))",
            name="ck_capacity_sleep_superseded_at",
        ),
        CheckConstraint(
            "image_evidence IN ('unknown', 'saved', 'failed', 'unavailable')",
            name="ck_capacity_sleep_image_evidence",
        ),
        CheckConstraint(
            "image_evidence != 'saved' OR (boot_id != '' AND provider_stopped_at IS NOT NULL "
            "AND coalesce(accepted_mode, requested_mode, '') = 'hibernate' "
            "AND marker_observed_at IS NOT NULL "
            "AND evidence_at IS NOT NULL)",
            name="ck_capacity_sleep_saved_evidence",
        ),
        CheckConstraint(
            "evidence_reason IS NULL OR evidence_reason IN ('save_completed', 'save_failed', "
            "'unsupported', 'plain_stop', 'evidence_missing', 'evidence_expired', "
            "'external_change', 'legacy_unknown', 'provider_rejected', 'forced_stop', "
            "'save_aborted')",
            name="ck_capacity_sleep_reason",
        ),
    )

    id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    instance_record_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("compute_provider_instances.id", ondelete="CASCADE"), nullable=False
    )
    boot_id: Mapped[str] = mapped_column(Text, nullable=False)
    requested_mode: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    marker_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_mode: Mapped[str | None] = mapped_column(Text)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    image_evidence: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_reason: Mapped[str | None] = mapped_column(Text)
