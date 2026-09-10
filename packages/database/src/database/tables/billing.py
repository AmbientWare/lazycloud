from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
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


class BillingAccountTable(IdTable, DatabaseBase):
    """The payment relationship behind every workspace one user owns.

    One per account rather than per workspace, matching
    `aws_account_connections`: an org running dev, staging and prod agreed to pay
    once, and a row per workspace would be several payment methods to keep in
    step by hand.

    A row appears when an account first signs in, before its session exists.
    It records the provider customer and any paid subscription. An empty `plan`
    means the account has no recorded plan.
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
        CheckConstraint(
            "plan IN ('', 'free', 'team', 'business')", name="ck_billing_accounts_plan"
        ),
        CheckConstraint(
            "subscription_terms_version IS NULL OR "
            "(plan = 'free' AND subscription_terms_version IN ('free-v1', 'free-v2')) OR "
            "(plan = 'team' AND subscription_terms_version "
            "IN ('team-v1', 'team-v2', 'team-v3')) OR "
            "(plan = 'business' AND subscription_terms_version IN ('business-v1', 'business-v2'))",
            name="ck_billing_accounts_subscription_terms",
        ),
        CheckConstraint(
            "(scheduled_terms_version IS NULL AND scheduled_change_at IS NULL) OR "
            "(scheduled_terms_version IS NOT NULL AND "
            "scheduled_terms_version IN "
            "('free-v1', 'team-v1', 'free-v2', 'team-v2', 'business-v1', 'team-v3', 'business-v2') "
            "AND scheduled_change_at IS NOT NULL)",
            name="ck_billing_accounts_scheduled_terms",
        ),
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
    """The paid plan subscription, empty when the account has none."""
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    """Which plan that subscription is on, empty while there is none. A column
    rather than a read of the provider because the dashboard and an upgrade both
    need it without a round trip, and it is cleared with the subscription so the
    two never disagree about what the account is on."""
    subscription_terms_version: Mapped[str | None] = mapped_column(String(32))
    scheduled_terms_version: Mapped[str | None] = mapped_column(String(32))
    scheduled_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_method_attached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    """When this account's card was put on file, null while it holds none.

    Nullable rather than defaulted to an instant, because every other column here
    says "not yet" with an empty string and no timestamp means the same. The
    distinction is load-bearing: it decides what the account may spend before
    anything can be charged, and a zero-ish default would read as a card attached
    at the epoch.

    Cleared when the last card is removed. Left set, an account could attach a
    card, spend against the larger allowance that buys, detach, and keep it."""
    complimentary_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    """When an administrator waived this account's bill, null while nobody has.

    Its own column beside the plan rather than a third plan value. A plan is
    something the provider prices and the rate card publishes, and this is a
    decision made here about who is not charged. Cleared when the waiver is
    withdrawn, at which point the subscription the row still names is what
    the account is on."""
