from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.email_outbox import EmailOutboxRepository
from database.types import DatabaseSession
from shared.email import EmailDeliveryState


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """One thing a provider says became of a message it accepted earlier."""

    provider_message_id: str
    state: EmailDeliveryState
    occurred_at: datetime
    detail: str = ""


def record_delivery(session: DatabaseSession, report: DeliveryReport) -> bool:
    """Write a delivery outcome against the message it belongs to.

    False where no message answers to that id, which is ordinary rather than an
    error: a provider retains its own history longer than this platform retains
    rows, so a delivery for something already gone is a report about nothing.
    """
    return EmailOutboxRepository(session).record_delivery(
        provider_message_id=report.provider_message_id,
        state=report.state,
        occurred_at=report.occurred_at,
        detail=report.detail,
    )


__all__ = ["DeliveryReport", "record_delivery"]
