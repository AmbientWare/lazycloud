from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable


class EmailOutboxTable(IdTable, DatabaseBase):
    """One message the platform owes somebody, written before anything sends it.

    Written in the transaction that decided to send it, so the thing the message
    is about and the message itself are either both recorded or neither is. A
    request never waits on the email provider: it commits the row and returns,
    and the drain is what talks to the provider. That is what keeps an outage at
    the provider from being an outage of the API that mentions it.

    The rendered message is stored rather than a reference to what produced it,
    for the same reason the meter outbox stores its event: the drain claims rows,
    sends them and marks them, and derives nothing. A message whose wording was
    recomputed at send time could go out saying something other than what the
    action that queued it meant.
    """

    __tablename__ = "email_outbox"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'abandoned')",
            name="ck_email_outbox_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_email_outbox_attempts"),
        # A claimed row cannot exist without its claim, and a claim cannot sit on
        # an unclaimed one, or a stale drainer's acknowledgement would land on a
        # message somebody else is already sending.
        CheckConstraint(
            "(status = 'sending') = (claim_token IS NOT NULL)",
            name="ck_email_outbox_claim",
        ),
        Index(
            "ix_email_outbox_ready",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index(
            "ix_email_outbox_stuck",
            "claimed_at",
            postgresql_where=text("status = 'sending'"),
            sqlite_where=text("status = 'sending'"),
        ),
        # What a delivery event names. Unique because two rows answering to one
        # provider id would make "which message bounced" have two answers.
        Index(
            "uq_email_outbox_provider_message",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id <> ''"),
            sqlite_where=text("provider_message_id <> ''"),
        ),
        CheckConstraint(
            "delivery_state IN ('queued', 'sent', 'delivered', 'bounced', 'complained', 'failed')",
            name="ck_email_outbox_delivery_state",
        ),
    )

    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    text_body: Mapped[str] = mapped_column(Text, nullable=False)
    """Both bodies, because a client that cannot render HTML shows the text one
    and a message carrying only HTML reads as empty there."""

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    """Why the last attempt failed, for an operator reading a stalled queue. The
    provider's own words, which name a rejected key or an unverified domain."""

    provider_message_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    """The provider's own id, written when it accepts the message. Delivery
    events name this and nothing else, so a message sent without one is one
    whose fate can never be attached to it."""

    delivery_state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    """What became of the message, as far as anybody has been told.

    Distinct from `status`, which is this platform's progress in handing the
    message over. A row can be `sent` and `bounced` at once: we did our part and
    nobody received it, and that pair is exactly what somebody needs to see.
    """

    delivery_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivery_detail: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    """What the provider said about a bounce, in its words."""

    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the bodies were emptied.

    Delivered messages keep their row and lose their contents. The row is the
    delivery record somebody reads later; the body held a working invitation
    link, and there is no reason for that to stay readable once the mail that
    carried it has arrived.
    """


__all__ = ["EmailOutboxTable"]
