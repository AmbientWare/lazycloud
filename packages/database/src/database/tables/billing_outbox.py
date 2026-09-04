from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, uuid_type


class BillingMeterOutboxTable(TimestampMixin, DatabaseBase):
    """One meter event owed to the payment provider.

    Written by the pricer in the transaction that wrote the ledger segments it
    sums, so a metered window is either recorded, priced and queued, or none of
    the three. The sweep derives nothing: it claims rows that already exist,
    sends them, and marks them. It never scans usage, never groups, and never
    computes a cost.
    """

    __tablename__ = "billing_meter_outbox"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("identifier", name="uq_billing_meter_outbox_identifier"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'abandoned', 'waived')",
            name="ck_billing_meter_outbox_status",
        ),
        CheckConstraint("value_nanos >= 0", name="ck_billing_meter_outbox_value"),
        CheckConstraint("attempts >= 0", name="ck_billing_meter_outbox_attempts"),
        # A claimed row cannot exist without its claim, and a claim cannot leak
        # onto an unclaimed one — otherwise a stale drainer's acknowledgement
        # would land on a row somebody else is already sending.
        CheckConstraint(
            "(status = 'sending') = (claim_token IS NOT NULL)",
            name="ck_billing_meter_outbox_claim",
        ),
        Index(
            "ix_billing_meter_outbox_ready",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index(
            "ix_billing_meter_outbox_stuck",
            "claimed_at",
            postgresql_where=text("status = 'sending'"),
            sqlite_where=text("status = 'sending'"),
        ),
        Index("ix_billing_meter_outbox_settled", "status", "updated_at"),
        # What the reconciler reads: one customer's window, to subtract what has
        # not reached the provider yet from the ledger it compares an invoice
        # against.
        Index("ix_billing_meter_outbox_customer_window", "provider_customer_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(
        uuid_type,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """`RESTRICT` because an unsent event is money already owed to the provider:
    removing the workspace row must fail rather than silently drop the debt."""

    identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    """The usage record's own id, which is already deterministic per metering
    window. Doubles as the provider's deduplication key, which is what makes
    at-least-once delivery safe without an exactly-once protocol."""

    provider_customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    """Captured at write time and never re-resolved. The row is a durable
    statement of what to send, and it must survive the account row changing."""

    meter_event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    value_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pricing_version: Mapped[str] = mapped_column(String(512), nullable=False)
    """Every published version the priced span drew on, in the order they took
    effect. Wider than the one version a ledger segment carries because a span
    that crossed a rate change was priced under each of them, and a label naming
    only one would be the provider's copy disagreeing with the segments."""

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    last_error: Mapped[str] = mapped_column(String(512), nullable=False, default="")


__all__ = ["BillingMeterOutboxTable"]
