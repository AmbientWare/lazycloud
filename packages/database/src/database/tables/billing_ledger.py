from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, uuid_type


class ContainerBillingShapeTable(TimestampMixin, DatabaseBase):
    """What the control plane placed, which is what prices.

    Written in the transaction that records the runtime worker. A worker token
    lives on a machine a customer has root on, so the labels a worker puts on a
    usage record decide attribution and never money; a container with no row here
    is refused rather than priced at zero.
    """

    __tablename__ = "container_billing_shapes"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "billing_owner IN ('platform_fleet', 'connected_cloud', 'self_hosted')",
            name="ck_container_billing_shapes_owner",
        ),
        CheckConstraint(
            "cpu_millicores >= 0 AND memory_mib >= 0 AND gpu_count >= 0",
            name="ck_container_billing_shapes_nonnegative",
        ),
    )

    container_id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    """Which container was placed. Deliberately not a foreign key.

    A shape is a billing fact and has to outlive the container row it describes:
    usage can still arrive after a container is gone, and a cascade would delete
    the only record of what was placed, leaving that usage to price against
    nothing. Deleting a container stays possible, and stops being a way to make
    compute free.
    """
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    billing_owner: Mapped[str] = mapped_column(String(40), nullable=False)
    gpu_type: Mapped[str] = mapped_column(String(64), nullable=False)
    cpu_millicores: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)


class BillingLedgerSegmentTable(TimestampMixin, DatabaseBase):
    """One priced slice of one component of one metered span. Append-only.

    Carries its own attribution — app, workload, task, worker, owner — so
    answering "which app cost what" reads these rows and nothing else. A segment
    is never updated or deleted, so a rate published later can never reprice
    usage a customer has already been shown.

    `quantity` in `quantity_unit` is the whole of what was charged for, and
    `component` says which resource it counts. There is no second denormalized
    breakdown beside it: one number in one place cannot disagree with itself, and
    a copy allocated from milliseconds could not express a burst above the
    reservation at all.
    """

    __tablename__ = "billing_ledger_segments"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "usage_record_id",
            "component",
            "segment_index",
            name="uq_billing_ledger_segments_record",
        ),
        CheckConstraint(
            "dimension IN ('compute_runtime', 'network_egress', 'volume_storage')",
            name="ck_billing_ledger_segments_dimension",
        ),
        CheckConstraint(
            "component IN ('container_time', 'cpu', 'memory', 'gpu', 'egress', 'volume_storage')",
            name="ck_billing_ledger_segments_component",
        ),
        CheckConstraint(
            "basis IN ('reserved', 'measured')",
            name="ck_billing_ledger_segments_basis",
        ),
        CheckConstraint(
            "segment_ended_at > segment_started_at",
            name="ck_billing_ledger_segments_interval",
        ),
        CheckConstraint("duration_ms >= 0", name="ck_billing_ledger_segments_duration"),
        CheckConstraint("cost_nanos >= 0", name="ck_billing_ledger_segments_cost"),
        CheckConstraint("rate_nanos_per_unit >= 0", name="ck_billing_ledger_segments_rate"),
        Index(
            "ix_billing_ledger_segments_workspace_time",
            "workspace_id",
            "segment_started_at",
        ),
        Index(
            "ix_billing_ledger_segments_workspace_app_time",
            "workspace_id",
            "app_id",
            "segment_started_at",
        ),
        Index(
            "ix_billing_ledger_segments_account_time",
            "owner_user_id",
            "segment_started_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        uuid_type,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    usage_record_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("usage_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """Where the cost was incurred. `RESTRICT` for the same reason as the payer
    below: a workspace goes inactive rather than away, and no admin path, cleanup
    job or test may turn removing its row into forgetting what it owed."""

    owner_user_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """Who pays. Denormalized so the allowance and the dashboard read without a
    two-hop join through workspace membership on every row."""

    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    component: Mapped[str] = mapped_column(String(32), nullable=False)
    basis: Mapped[str] = mapped_column(String(16), nullable=False)
    """Whether this quantity is capacity held or capacity measured.

    A container billed only on `reserved` CPU had no measurement behind it — the
    routine case for an image build, and for any window that stayed under what it
    reserved. One with both rows burst past its reservation and paid for the
    excess.
    """

    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(160), nullable=False)

    app_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    workload_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    task_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    worker_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    billing_owner: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    gpu_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    span_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    span_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    segment_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    segment_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 9), nullable=False)
    quantity_unit: Mapped[str] = mapped_column(String(24), nullable=False)

    pricing_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rate_nanos_per_unit: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    quote_effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quote_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cost_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = ["BillingLedgerSegmentTable", "ContainerBillingShapeTable"]
