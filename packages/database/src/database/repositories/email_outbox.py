from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from database.tables.email_outbox import EmailOutboxTable
from shared.email import EmailDeliveryState, EmailMessage
from shared.timestamps import to_utc, utc_now
from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ClaimedEmail:
    """A message this drainer holds, and the attempt it is spending on it."""

    id: str
    message: EmailMessage
    attempts: int


@dataclass(slots=True)
class EmailOutboxRepository:
    session: Session

    def enqueue(self, message: EmailMessage, *, now: datetime) -> str:
        """Queue a message inside the caller's transaction.

        No commit here. The point of the outbox is that the message and whatever
        it announces land together, so committing early would be able to send
        mail about something that then rolled back.
        """
        row = EmailOutboxTable(
            recipient=message.to,
            subject=message.subject,
            html_body=message.html,
            text_body=message.text,
            status="pending",
            next_attempt_at=now,
        )
        self.session.add(row)
        self.session.flush()
        return str(row.id)

    def claim(self, *, now: datetime, limit: int, claim_token: str) -> tuple[ClaimedEmail, ...]:
        """Take up to `limit` due rows for this drainer alone.

        `FOR UPDATE SKIP LOCKED` is what makes several drainers safe with no
        lease: a row another drainer holds is passed over rather than waited on.
        The attempt is spent at claim rather than at failure, so a drainer that
        dies mid-send still burns one and cannot spin on the same row forever.
        """
        if limit <= 0:
            return ()
        due = (
            select(EmailOutboxTable.id)
            .where(
                EmailOutboxTable.status == "pending",
                EmailOutboxTable.next_attempt_at <= now,
            )
            .order_by(EmailOutboxTable.next_attempt_at, EmailOutboxTable.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed = self.session.execute(
            update(EmailOutboxTable)
            .where(EmailOutboxTable.id.in_(due.scalar_subquery()))
            .values(
                status="sending",
                claim_token=claim_token,
                claimed_at=now,
                attempts=EmailOutboxTable.attempts + 1,
                updated_at=now,
            )
            .returning(
                EmailOutboxTable.id,
                EmailOutboxTable.recipient,
                EmailOutboxTable.subject,
                EmailOutboxTable.html_body,
                EmailOutboxTable.text_body,
                EmailOutboxTable.attempts,
            )
            .execution_options(synchronize_session=False)
        ).all()
        return tuple(
            ClaimedEmail(
                id=str(row.id),
                message=EmailMessage(
                    to=row.recipient,
                    subject=row.subject,
                    html=row.html_body,
                    text=row.text_body,
                ),
                attempts=row.attempts,
            )
            for row in claimed
        )

    def mark_sent(
        self,
        *,
        message_id: str,
        claim_token: str,
        now: datetime,
        provider_message_id: str,
    ) -> bool:
        """Record that the provider took it, and under which id it will report back."""
        return self._settle(
            message_id=message_id,
            claim_token=claim_token,
            values={
                "status": "sent",
                "sent_at": now,
                "claim_token": None,
                "claimed_at": None,
                "provider_message_id": provider_message_id,
                "delivery_state": EmailDeliveryState.Sent.value,
            },
            now=now,
        )

    def mark_failed(
        self,
        *,
        message_id: str,
        claim_token: str,
        now: datetime,
        next_attempt_at: datetime,
        error: str,
    ) -> bool:
        return self._settle(
            message_id=message_id,
            claim_token=claim_token,
            values={
                "status": "pending",
                "next_attempt_at": next_attempt_at,
                "claim_token": None,
                "claimed_at": None,
                "last_error": error[:500],
            },
            now=now,
        )

    def abandon(self, *, message_id: str, claim_token: str, now: datetime, error: str) -> bool:
        """Stop trying. The row stays as the record that somebody was never told."""
        return self._settle(
            message_id=message_id,
            claim_token=claim_token,
            values={
                "status": "abandoned",
                "claim_token": None,
                "claimed_at": None,
                "last_error": error[:500],
                "delivery_state": EmailDeliveryState.Failed.value,
            },
            now=now,
        )

    def reclaim(self, *, now: datetime, claimed_before: datetime) -> int:
        """Return rows a drainer took and never settled, so another can try them.

        A process killed mid-send leaves its claim behind; without this the
        message waits for a drainer that no longer exists.
        """
        result = self.session.execute(
            update(EmailOutboxTable)
            .where(
                EmailOutboxTable.status == "sending",
                EmailOutboxTable.claimed_at < claimed_before,
            )
            .values(
                status="pending",
                claim_token=None,
                claimed_at=None,
                next_attempt_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)

    def abandoned_total(self) -> int:
        """Messages given up on, which is the figure that matters during an outage."""
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(EmailOutboxTable)
                .where(EmailOutboxTable.status == "abandoned")
            )
            or 0
        )

    def redact(self, *, sent_before: datetime, limit: int, now: datetime) -> int:
        """Empty the bodies of messages already handed over, keeping the row.

        The body holds a working invitation link and the row is the delivery
        record somebody reads afterwards, so the two are cleared separately: the
        secret goes once the mail carrying it has gone, and what became of the
        message stays.
        """
        doomed = (
            select(EmailOutboxTable.id)
            .where(
                EmailOutboxTable.status == "sent",
                EmailOutboxTable.sent_at < sent_before,
                EmailOutboxTable.redacted_at.is_(None),
            )
            .limit(limit)
        )
        result = self.session.execute(
            update(EmailOutboxTable)
            .where(EmailOutboxTable.id.in_(doomed.scalar_subquery()))
            .values(html_body="", text_body="", redacted_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)

    def record_delivery(
        self,
        *,
        provider_message_id: str,
        state: EmailDeliveryState,
        occurred_at: datetime,
        detail: str = "",
    ) -> bool:
        """Write what a provider says became of a message it already accepted.

        Ordered on the event's own timestamp rather than on arrival, because
        delivery events are not promised in order and a late `sent` landing
        after a `bounced` would otherwise erase the outcome that mattered.
        """
        result = self.session.execute(
            update(EmailOutboxTable)
            .where(
                EmailOutboxTable.provider_message_id == provider_message_id,
                or_(
                    EmailOutboxTable.delivery_event_at.is_(None),
                    EmailOutboxTable.delivery_event_at <= occurred_at,
                ),
            )
            .values(
                delivery_state=state.value,
                delivery_event_at=occurred_at,
                delivery_detail=detail[:500],
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) > 0

    def delivery_states(self, message_ids: Collection[str]) -> dict[str, EmailDeliveryState]:
        """What became of each of these messages, for a listing that shows it."""
        if not message_ids:
            return {}
        rows = self.session.execute(
            select(EmailOutboxTable.id, EmailOutboxTable.delivery_state).where(
                EmailOutboxTable.id.in_(list(message_ids))
            )
        ).all()
        return {str(row.id): EmailDeliveryState(row.delivery_state) for row in rows}

    def get_status(self, message_id: str) -> tuple[str, int, datetime | None] | None:
        row = self.session.get(EmailOutboxTable, message_id)
        if row is None:
            return None
        return row.status, row.attempts, to_utc(row.sent_at) if row.sent_at else None

    def _settle(
        self,
        *,
        message_id: str,
        claim_token: str,
        values: dict[str, object],
        now: datetime,
    ) -> bool:
        result = self.session.execute(
            update(EmailOutboxTable)
            .where(
                EmailOutboxTable.id == message_id,
                EmailOutboxTable.claim_token == claim_token,
            )
            .values(updated_at=now, **values)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) > 0


def _rowcount(result: object) -> int:
    return int(result.rowcount) if isinstance(result, CursorResult) else 0


__all__ = ["ClaimedEmail", "EmailOutboxRepository"]
