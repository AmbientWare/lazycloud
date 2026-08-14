from __future__ import annotations

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    literal_column,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import (
    DatabaseBase,
    IdPayloadTable,
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
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """`RESTRICT` because the priced ledger derives from these rows and outlives
    the workspace: removing the workspace row must fail rather than take the
    evidence for a charge with it."""

    resource_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)


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
