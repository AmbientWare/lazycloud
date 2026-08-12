from __future__ import annotations

from dataclasses import dataclass

from database.tables.billing_webhook_events import BillingWebhookEventTable
from shared.timestamps import utc_now
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class BillingWebhookEventRepository:
    session: Session

    def claim(self, *, event_id: str, event_type: str) -> bool:
        """Take this event, or report that someone already has it.

        The insert is the claim. Checking for the row and then writing it would
        leave the two deliveries of one retried event both finding nothing and
        both proceeding — which is the case this exists for, not an unlikely one:
        a slow handler is exactly what makes the provider send the event again.

        The caller must commit the claim in the same transaction as whatever it
        does with the event. A claim committed on its own would swallow the event
        if the work after it failed.
        """

        try:
            # Nested, so a duplicate rolls back only this insert. A plain rollback
            # would discard the caller's whole transaction, and this is the
            # deduplication primitive for money events — every caller has work in
            # flight when it asks.
            with self.session.begin_nested():
                self.session.add(
                    BillingWebhookEventTable(
                        event_id=event_id, event_type=event_type, received_at=utc_now()
                    )
                )
        except IntegrityError:
            return False
        return True


__all__ = ["BillingWebhookEventRepository"]
