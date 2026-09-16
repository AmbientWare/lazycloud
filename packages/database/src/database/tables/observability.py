from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    literal_column,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import (
    DatabaseBase,
    IdTable,
    json_type,
    uuid_type,
)


class WorkerEventTable(IdTable, DatabaseBase):
    __tablename__ = "worker_events"
    event_data: Mapped[dict[str, JsonValue]] = mapped_column(json_type, default=dict)
    __table_args__: tuple[SchemaItem, ...] = (
        Index("ix_worker_events_worker_created", "worker_id", "created_at"),
        Index("ix_worker_events_type_created", "event_type", "created_at"),
        Index("ix_worker_events_resource", "resource_id"),
        Index("ix_worker_events_created", "created_at"),
    )

    worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(160), nullable=True)


class UsageRecordTable(IdTable, DatabaseBase):
    __tablename__ = "usage_records"
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    labels: Mapped[dict[str, str]] = mapped_column(json_type, default=dict)
    metadata_json: Mapped[dict[str, JsonValue]] = mapped_column("metadata", json_type, default=dict)
    metering_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metering_ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    app_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    stub_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    deployment_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    gpu: Mapped[str | None] = mapped_column(String(160), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "quantity >= 0 AND quantity < 'Infinity'::float8", name="ck_usage_records_quantity"
        ),
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
