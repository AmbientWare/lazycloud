from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable, uuid_type


class BillingPeriodTable(IdTable, DatabaseBase):
    """One account's bill for one month, and how far it has got towards being paid.

    Scoped to the payer, not the workspace: an account pays once for every
    workspace its owner holds.

    What it owes is frozen onto the row when it closes — the plan, its price, its
    allowance, and what the usage came to. The ledger it was built from keeps
    moving; this does not. That is the only place in this schema where a number
    stops being a question and becomes what somebody was charged.
    """

    __tablename__ = "billing_periods"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("user_id", "period_start", name="uq_billing_periods_account_month"),
        CheckConstraint("period_end > period_start", name="ck_billing_periods_interval"),
        CheckConstraint("usage_cost_nanos >= 0", name="ck_billing_periods_usage"),
        CheckConstraint("charged_cost_nanos >= 0", name="ck_billing_periods_charged"),
        CheckConstraint("included_cost_nanos >= 0", name="ck_billing_periods_included"),
        CheckConstraint("subscription_cost_nanos >= 0", name="ck_billing_periods_subscription"),
        CheckConstraint(
            "status IN ('open', 'closed', 'invoiced', 'nothing_owed', "
            "'paid', 'payment_failed', 'voided')",
            name="ck_billing_periods_status",
        ),
        # An amount nobody can point at a document is one nobody can dispute,
        # refund, or reconcile.
        CheckConstraint(
            "status NOT IN ('invoiced', 'paid', 'payment_failed', 'voided') "
            "OR provider_invoice_id <> ''",
            name="ck_billing_periods_invoiced_names_invoice",
        ),
        # The other half: a period that finished owing nothing sent nothing, so
        # an invoice id on one names a document this platform did not issue.
        CheckConstraint(
            "status <> 'nothing_owed' OR provider_invoice_id = ''",
            name="ck_billing_periods_nothing_owed_has_no_invoice",
        ),
        CheckConstraint("plan IN ('free', 'team')", name="ck_billing_periods_plan"),
        # A payment outcome names the invoice and nothing else, so this is what
        # it is resolved through. Unique for the same reason as the account's
        # customer id — two periods claiming one invoice would apply a payment to
        # whichever came back — and partial because a period holds the empty
        # string until it is issued.
        Index(
            "uq_billing_periods_provider_invoice",
            "provider_invoice_id",
            unique=True,
            postgresql_where=text("provider_invoice_id <> ''"),
            sqlite_where=text("provider_invoice_id <> ''"),
        ),
    )

    user_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """Restricted, never cascaded: what an account was charged outlives the
    account. Nothing hard-deletes a user today, so this is what keeps that
    true rather than a rule anybody has to remember."""

    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    plan: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payment_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When collection was last tried, null where it never has been.

    Its own column rather than reading `updated_at`, because a retry policy built
    on when the row last changed would move every time anything else touched it,
    and would silently stop meaning what it was written to mean."""
    usage_cost_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    included_cost_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    subscription_cost_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    charged_cost_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    provider_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
