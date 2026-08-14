from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, uuid_type


class BillingPlanChangeIntentTable(TimestampMixin, DatabaseBase):
    """One plan change this platform means to make at the payment provider.

    Written and committed before the provider is called, and settled from what
    the provider says afterwards. The provider cannot join a transaction here, so
    without this row the window between the swap being charged and the account
    row naming the plan is a window nothing can recover from: the customer has
    paid, the row still names the plan they left, and nothing knows to ask.

    The subscription and the customer are captured at write time and never
    re-resolved, for the reason the outbox does the same — the row is a durable
    statement of what was attempted and must survive the account row changing.
    """

    __tablename__ = "billing_plan_change_intents"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "status IN ('pending', 'settling', 'applied', 'not_applied', 'abandoned')",
            name="ck_billing_plan_change_intents_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_billing_plan_change_intents_attempts"),
        # A claimed row cannot exist without its claim, and a claim cannot leak
        # onto an unclaimed one — otherwise a stale settler's write would land on
        # an intent somebody else is already settling.
        CheckConstraint(
            "(status = 'settling') = (claim_token IS NOT NULL)",
            name="ck_billing_plan_change_intents_claim",
        ),
        # At most one plan change open per account, in the schema rather than in
        # a lock. Splitting the request across two transactions is what gives up
        # the account row lock that used to serialize two simultaneous
        # subscribes, and two of those are two prorations charged for one
        # upgrade.
        Index(
            "uq_billing_plan_change_intents_open",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'settling')"),
            sqlite_where=text("status IN ('pending', 'settling')"),
        ),
        Index(
            "ix_billing_plan_change_intents_ready",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index(
            "ix_billing_plan_change_intents_stuck",
            "claimed_at",
            postgresql_where=text("status = 'settling'"),
            sqlite_where=text("status = 'settling'"),
        ),
    )

    id: Mapped[str] = mapped_column(
        uuid_type,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """`RESTRICT` because an unsettled intent is a charge that may already have
    been taken: removing the payer must fail rather than drop the only record
    that anything was attempted."""

    provider_customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_subscription_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_plan: Mapped[str] = mapped_column(String(32), nullable=False)
    """The plan the change was for. What the provider's subscription reads is
    compared against this, and the two together are the whole verdict: equal
    means the swap happened and was paid for, unequal means it did not."""

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(uuid_type, nullable=True)
    last_error: Mapped[str] = mapped_column(String(512), nullable=False, default="")


__all__ = ["BillingPlanChangeIntentTable"]
