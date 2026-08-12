from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdPayloadTable, uuid_type


class BillingAccountTable(IdPayloadTable, DatabaseBase):
    """The payment relationship behind every workspace one user owns.

    One per account rather than per workspace, matching
    `aws_account_connections`: an org running dev, staging and prod agreed to pay
    once, and a row per workspace would be several payment methods to keep in
    step by hand.

    A row appears only when an account first agrees to pay. Absence is the free
    plan, so nothing writes here to record that someone is not paying.
    """

    __tablename__ = "billing_accounts"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("user_id", name="uq_billing_accounts_user"),
        # These two decide what an account is charged and what it may run, so the
        # schema holds them rather than trusting every writer to.
        CheckConstraint("plan IN ('free', 'team')", name="ck_billing_accounts_plan"),
        CheckConstraint("status IN ('active', 'past_due')", name="ck_billing_accounts_status"),
        # A customer belongs to one account. Two rows claiming one would make
        # every question asked of the provider about that customer ambiguous, and
        # the answer would be whichever row came back first. Partial because the
        # empty string is how an account with no payment relationship yet says
        # so, and every one of those would otherwise collide.
        Index(
            "uq_billing_accounts_provider_customer",
            "provider_customer_id",
            unique=True,
            postgresql_where=text("provider_customer_id <> ''"),
            sqlite_where=text("provider_customer_id <> ''"),
        ),
    )

    user_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """Restricted, never cascaded. A billing account is evidence of a payment
    relationship, and deleting the user must not take it with them."""
    plan: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_customer_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    """A column rather than payload-only because a webhook names the customer and
    nothing else, so resolving one back to an account has to filter on it."""
