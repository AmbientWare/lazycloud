from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, json_type, uuid_type


class BillingFundingHoldTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_funding_holds"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("revision >= 1", name="ck_billing_funding_holds_revision"),
        CheckConstraint(
            "cpu_millicores >= 0 AND memory_mib >= 0 AND gpu_count >= 0",
            name="ck_billing_funding_holds_resources",
        ),
        CheckConstraint(
            "(authorized_at IS NULL AND valid_until IS NULL) OR "
            "(authorized_at IS NOT NULL AND valid_until IS NOT NULL "
            "AND valid_until > authorized_at)",
            name="ck_billing_funding_holds_permit",
        ),
        CheckConstraint(
            "terminal_at IS NULL OR (authorized_at IS NOT NULL AND terminal_at >= authorized_at)",
            name="ck_billing_funding_holds_terminal",
        ),
        Index("ix_billing_funding_holds_account", "user_id", "terminal_at"),
        CheckConstraint(
            "loss_resolved_at IS NULL OR (loss_machine_id IS NOT NULL "
            "AND loss_provider_instance_id IS NOT NULL AND loss_evidence_at IS NOT NULL "
            "AND loss_exposure_nanos IS NOT NULL AND loss_exposure_nanos >= 0)",
            name="ck_billing_funding_holds_loss_evidence",
        ),
    )

    container_id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    worker_id: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    billing_owner: Mapped[str] = mapped_column(String(40), nullable=False)
    rate_class: Mapped[str] = mapped_column(String(64), nullable=False)
    gpu_type: Mapped[str] = mapped_column(String(64), nullable=False)
    cpu_millicores: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metered_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    loss_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    loss_machine_id: Mapped[str | None] = mapped_column(uuid_type)
    loss_provider_instance_id: Mapped[str | None] = mapped_column(uuid_type)
    loss_evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    loss_exposure_nanos: Mapped[int | None] = mapped_column(BigInteger)


class BillingFundingAllocationTable(DatabaseBase):
    __tablename__ = "billing_funding_allocations"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("amount_nanos > 0", name="ck_billing_funding_allocations_amount"),
        Index("ix_billing_funding_allocations_lot", "credit_lot_id"),
    )

    container_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("billing_funding_holds.container_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    credit_lot_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("billing_credit_lots.id", ondelete="RESTRICT"), primary_key=True
    )
    amount_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)


class BillingFundingWindowTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_funding_windows"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("ended_at > started_at", name="ck_billing_funding_windows_interval"),
    )

    container_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("billing_funding_holds.container_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usage_record_ids: Mapped[list[JsonValue]] = mapped_column(json_type, nullable=False)


__all__ = ["BillingFundingAllocationTable", "BillingFundingHoldTable", "BillingFundingWindowTable"]
