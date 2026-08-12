from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from database.tables.base import DatabaseBase


class BillingWebhookEventTable(DatabaseBase):
    """That one payment-provider event has already been acted on.

    Deliveries repeat. The provider retries anything it does not get a 2xx for,
    retries again if the acknowledgement is lost on the way back, and can send
    the same event twice for reasons of its own — so "did this arrive already" is
    a question every delivery has to answer before it changes anything.

    Durable rather than a cache, because what it guards is durable: an event
    applied twice moves an account's standing or a period's outcome twice, and a
    record that expired at the wrong moment would let a week-old retry do it
    again. The rows are small and one per event.

    Keyed on the provider's own event id, which is the only identifier both sides
    agree on — the platform never sees the delivery attempt, only the event.
    """

    __tablename__ = "billing_webhook_events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When this platform first accepted it. Kept because the provider's retry
    window and this table are the two halves of any argument about a delivery
    that appears to have been missed."""
