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

from database.tables.base import DatabaseBase, IdTable, uuid_type


class BillingAccountTable(IdTable, DatabaseBase):
    """The payment relationship behind every workspace one user owns.

    One per account rather than per workspace, matching
    `aws_account_connections`: an org running dev, staging and prod agreed to pay
    once, and a row per workspace would be several payment methods to keep in
    step by hand.

    A row appears when an account first signs in, before its session exists, and
    names the provider's customer, subscription and grant from the moment that
    provisioning finishes. An empty `plan` means an account on no subscription —
    one that has never reached a billing surface, or one whose subscription the
    provider says has ended — and so does the absence of a row.
    """

    __tablename__ = "billing_accounts"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("user_id", name="uq_billing_accounts_user"),
        # Standing decides what an account may run, so the schema holds it rather
        # than trusting every writer to.
        CheckConstraint("status IN ('active', 'past_due')", name="ck_billing_accounts_status"),
        # The plan decides what the account's period is worth and what its grant
        # is sized to, so the schema holds the vocabulary rather than trusting
        # every writer to. The empty string is the unprovisioned row, the same
        # way it is for the provider identifiers beside it.
        CheckConstraint("plan IN ('', 'free', 'team')", name="ck_billing_accounts_plan"),
        # A customer belongs to one account. Two rows claiming one would make
        # every question asked of the provider about that customer ambiguous, and
        # the answer would be whichever row came back first. Partial because the
        # empty string is how a row whose registration has not finished says it
        # names nobody yet, and every one of those would otherwise collide.
        Index(
            "uq_billing_accounts_provider_customer",
            "provider_customer_id",
            unique=True,
            postgresql_where=text("provider_customer_id <> ''"),
            sqlite_where=text("provider_customer_id <> ''"),
        ),
        Index(
            "uq_billing_accounts_provider_subscription",
            "provider_subscription_id",
            unique=True,
            postgresql_where=text("provider_subscription_id <> ''"),
            sqlite_where=text("provider_subscription_id <> ''"),
        ),
        Index(
            "uq_billing_accounts_provider_credit_grant",
            "provider_credit_grant_id",
            unique=True,
            postgresql_where=text("provider_credit_grant_id <> ''"),
            sqlite_where=text("provider_credit_grant_id <> ''"),
        ),
    )

    user_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """Restricted, never cascaded. A billing account is evidence of a payment
    relationship, and deleting the user must not take it with them."""
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_customer_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    """A column rather than payload-only because a webhook names the customer and
    nothing else, so resolving one back to an account has to filter on it."""
    provider_subscription_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    """The subscription that carries the plan price and the metered prices, empty
    until the account is provisioned."""
    provider_credit_grant_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    """The grant that carries the included allowance for the current period,
    empty until one is issued."""
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    """Which plan that subscription is on, empty while there is none. A column
    rather than a read of the provider because the dashboard and an upgrade both
    need it without a round trip, and it is cleared with the subscription so the
    two never disagree about what the account is on."""
