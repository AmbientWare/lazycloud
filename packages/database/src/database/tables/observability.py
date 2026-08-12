from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    literal_column,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import (
    DatabaseBase,
    IdPayloadTable,
    TimestampMixin,
    uuid_type,
)


class WorkerEventTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "worker_events"
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_worker_events_worker_created", "worker_id", "created_at"),
        Index("ix_worker_events_type_created", "event_type", "created_at"),
        Index("ix_worker_events_resource", "resource_id"),
        Index("ix_worker_events_created", "created_at"),
    )

    worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(160), nullable=True)


class UsageRecordTable(IdPayloadTable, DatabaseBase):
    __tablename__ = "usage_records"
    __table_args__: tuple[SchemaItem, ...] = (
        Index(
            "ix_usage_records_workspace_created",
            "workspace_id",
            literal_column("created_at", DateTime(timezone=True)).desc(),
            literal_column("id", String).asc(),
        ),
        Index(
            "ix_usage_records_workspace_metric_created",
            "workspace_id",
            "metric",
            literal_column("created_at", DateTime(timezone=True)).desc(),
            literal_column("id", String).asc(),
        ),
        Index(
            "ix_usage_records_workspace_resource_created",
            "workspace_id",
            "resource_type",
            "resource_id",
            literal_column("created_at", DateTime(timezone=True)).desc(),
            literal_column("id", String).asc(),
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)


class UsageBillingWindowTable(TimestampMixin, DatabaseBase):
    __tablename__ = "usage_billing_windows"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "workspace_id",
            "app_id",
            "workload_id",
            "resource_id",
            "worker_id",
            "window_start_ms",
            "window_end_ms",
            "legacy_record_id",
            name="uq_usage_billing_windows_identity",
        ),
        Index(
            "ix_usage_billing_windows_workspace_billing",
            "workspace_id",
            "billing_at",
        ),
        Index(
            "ix_usage_billing_windows_workspace_app_billing",
            "workspace_id",
            "app_id",
            "billing_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        uuid_type,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    workload_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    billing_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    window_start_ms: Mapped[int] = mapped_column(BigInteger, default=-1, nullable=False)
    window_end_ms: Mapped[int] = mapped_column(BigInteger, default=-1, nullable=False)
    legacy_record_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    cpu_direct_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cpu_direct_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    cpu_derived_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    memory_direct_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    memory_direct_gib_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    memory_derived_gib_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    billing_owner: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    gpu_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    gpu_direct_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gpu_direct_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    gpu_derived_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    omitted_duration_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class UsageBillingContributionTable(TimestampMixin, DatabaseBase):
    __tablename__ = "usage_billing_contributions"
    __table_args__: tuple[SchemaItem, ...] = (
        Index(
            "ix_usage_billing_contributions_window",
            "workspace_id",
            "app_id",
            "workload_id",
            "resource_id",
            "worker_id",
            "window_start_ms",
            "window_end_ms",
            "legacy_record_id",
        ),
    )

    record_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("usage_records.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    workload_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    billing_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    window_start_ms: Mapped[int] = mapped_column(BigInteger, default=-1, nullable=False)
    window_end_ms: Mapped[int] = mapped_column(BigInteger, default=-1, nullable=False)
    legacy_record_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    cpu_direct_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cpu_direct_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    cpu_derived_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    memory_direct_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    memory_direct_gib_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    memory_derived_gib_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    billing_owner: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    """Who paid for the machine, which decides whether and how the window prices.

    An attribute for the same reason as `gpu_type`: a window is one container on
    one worker in one unit, so every record in it answers this identically. In
    the identity it would enforce nothing and would let one stale-labelled record
    open a second window row for the same interval instead of correcting the
    first.

    Empty is a fourth value and not an error: a task count is not machine time
    and carries no owner. Anything pricing from this column has to answer for
    empty rather than assume the column holds one of the three names.
    """

    gpu_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    """GPU model the container held, empty for CPU-only work.

    An attribute of the window rather than part of its identity: a window is one
    container's metering interval and a container holds one GPU model for its
    whole life, so this can never split a window in two.
    """
    gpu_direct_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gpu_direct_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    gpu_derived_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    omitted_duration_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class MetricTable(IdPayloadTable, DatabaseBase):
    """Latest state per metric: one row per (kind, name, labels_key)."""

    __tablename__ = "metrics"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("kind", "name", "labels_key", name="uq_metrics_kind_name_labels"),
        Index("ix_metrics_kind_name", "kind", "name"),
    )

    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    labels_key: Mapped[str] = mapped_column(String(512), nullable=False, default="")
