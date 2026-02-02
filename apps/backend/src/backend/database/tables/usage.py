import uuid
from datetime import date, datetime

from models.usage import DailyUsageStatus
from sqlalchemy import (
    UUID,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.tables.base import (
    BaseTable,
)


class DailyUsageRecordTable(BaseTable):
    """One record per workspace per day - totals updated via atomic increment."""

    __tablename__ = "daily_usage_records"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True
    )
    usage_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(
        String, default=DailyUsageStatus.COLLECTING.value
    )

    # Totals - updated atomically via SQL increment
    cpu_core_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    memory_gb_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    storage_gb_months: Mapped[float] = mapped_column(Float, default=0.0)
    build_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    public_endpoint_hours: Mapped[float] = mapped_column(Float, default=0.0)

    # Tracking
    intervals_collected: Mapped[int] = mapped_column(Integer, default=0)
    expected_intervals: Mapped[int] = mapped_column(Integer, default=96)

    # Billing
    billing_id: Mapped[str | None] = mapped_column(String, nullable=True)
    billed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Billing attempts tracking
    billing_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_billing_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_billing_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "usage_date", name="uq_daily_usage_workspace_date"
        ),
        Index("ix_daily_usage_status_date", "status", "usage_date"),
    )


class CollectedIntervalTable(BaseTable):
    """Tracks which intervals have been collected - enables idempotency."""

    __tablename__ = "collected_intervals"

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    interval_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "interval_start", name="uq_collected_interval"
        ),
    )


class UsageBreakdownEventTable(BaseTable):
    """Append-only breakdown events for dashboard queries. Never updated."""

    __tablename__ = "usage_breakdown_events"

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    interval_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    breakdown_type: Mapped[str] = mapped_column(String, index=True)
    resource_name: Mapped[str] = mapped_column(String, index=True)
    service_name: Mapped[str | None] = mapped_column(String, nullable=True)
    storage_class: Mapped[str | None] = mapped_column(String, nullable=True)

    cpu_core_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    memory_gb_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    gb_hours: Mapped[float] = mapped_column(Float, default=0.0)
    endpoint_hours: Mapped[float] = mapped_column(Float, default=0.0)
    build_minutes: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        Index(
            "ix_breakdown_events_query",
            "workspace_id",
            "interval_start",
            "breakdown_type",
        ),
        Index("ix_breakdown_events_deployment", "deployment_id", "interval_start"),
    )
