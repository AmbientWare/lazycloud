from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from uuid import uuid4

from database.client import DatabaseClient
from database.repositories.email_outbox import ClaimedEmail, EmailOutboxRepository
from database.types import DatabaseSession
from shared.email import EmailMessage, EmailSender
from shared.errors import InvalidInputError
from shared.timestamps import utc_now

LOGGER = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
"""Attempts a message is given before nobody is told.

With the backoff below this spans about half an hour. Longer would not help: the
failures that outlast it are a rejected key, an unverified sending domain and a
malformed recipient, and none of those is fixed by waiting.
"""

CLAIM_TTL = timedelta(minutes=5)
SENT_RETENTION = timedelta(days=2)
"""How long a sent message keeps its body.

Short, because the body holds a working invitation link and the mail carrying it
has already gone. The row itself stays: it is the record of what became of the
message, which somebody reads long after the contents stop mattering.
"""

_RETRY_BASE = timedelta(seconds=10)
_RETRY_CAP = timedelta(seconds=600)

SWEEP_BUDGET_SECONDS = 10.0
"""Wall clock one sweep may spend talking to the provider.

Resend answers a send in about 0.2s measured, so a full batch is nearer five
seconds than this and the budget does not normally bind. It is here for the
degraded case rather than the ordinary one: sends are serial, the sweep shares a
housekeeping pass with the meter drain and domain reconciliation, and without a
bound a provider answering slowly rather than failing would push all of that
behind it. Messages left over are not lost. They keep their claim until it ages
out, and the next sweep takes them.
"""


@dataclass(frozen=True, slots=True)
class EmailDrainResult:
    """What one sweep did. Every figure is this sweep's own work."""

    sent_count: int = 0
    retried_count: int = 0
    abandoned_count: int = 0


class _Verdict(Enum):
    Sent = auto()
    Retry = auto()
    Abandon = auto()


def enqueue_email(
    session: DatabaseSession,
    message: EmailMessage,
    *,
    now: datetime | None = None,
) -> str:
    """Queue a message in the caller's transaction, and return its outbox id.

    A function rather than a service because there is no decision here to own:
    whoever is writing the thing the message announces writes the message beside
    it, and the two commit together or neither does.
    """
    return EmailOutboxRepository(session).enqueue(message, now=now or utc_now())


@dataclass(slots=True)
class EmailOutboxDrain:
    """Hand the provider the messages that are already owed.

    Holds a factory rather than a sender so a deployment with no email
    credential still starts. The failure then lands here, on a message that
    stays queued, instead of at boot on a process that has nothing else wrong
    with it.
    """

    database: DatabaseClient
    sender_factory: Callable[[], EmailSender]
    batch_size: int = 25
    sweep_budget_seconds: float = SWEEP_BUDGET_SECONDS
    monotonic: Callable[[], float] = time.monotonic

    def drain(self, *, now: datetime | None = None) -> EmailDrainResult:
        current = now or utc_now()
        claim_token = uuid4().hex
        with self.database.session() as session:
            EmailOutboxRepository(session).reclaim(
                now=current,
                claimed_before=current - CLAIM_TTL,
            )
        with self.database.session() as session:
            claimed = EmailOutboxRepository(session).claim(
                now=current,
                limit=self.batch_size,
                claim_token=claim_token,
            )
        if not claimed:
            return EmailDrainResult()

        try:
            sender = self.sender_factory()
        except Exception as exc:
            # No provider at all. Every claimed row goes back rather than
            # burning its attempts against a credential nobody has configured.
            self._release(claimed, claim_token=claim_token, now=current, error=str(exc))
            LOGGER.error("email cannot be delivered: %s", exc)
            return EmailDrainResult(retried_count=len(claimed))

        sent = retried = abandoned = 0
        deadline = self.monotonic() + self.sweep_budget_seconds
        for index, item in enumerate(claimed):
            if index and self.monotonic() >= deadline:
                # Out of time. The rest keep this sweep's claim, which ages out
                # on CLAIM_TTL and returns them; leaving them claimed rather
                # than settling them is what stops the same slow batch being
                # retried in a tight loop. Never on the first message, so a
                # provider slower than the whole budget still makes progress.
                LOGGER.warning(
                    "email sweep ran out of time with %d messages unsent",
                    len(claimed) - index,
                )
                break
            verdict, error, provider_message_id = self._deliver(sender, item)
            with self.database.session() as session:
                repository = EmailOutboxRepository(session)
                if verdict is _Verdict.Sent:
                    repository.mark_sent(
                        message_id=item.id,
                        claim_token=claim_token,
                        now=current,
                        provider_message_id=provider_message_id,
                    )
                    sent += 1
                elif verdict is _Verdict.Abandon:
                    repository.abandon(
                        message_id=item.id,
                        claim_token=claim_token,
                        now=current,
                        error=error,
                    )
                    abandoned += 1
                    LOGGER.error(
                        "email to %s abandoned after %d attempts: %s",
                        item.message.to,
                        item.attempts,
                        error,
                    )
                else:
                    repository.mark_failed(
                        message_id=item.id,
                        claim_token=claim_token,
                        now=current,
                        next_attempt_at=next_attempt_at(item.attempts, now=current),
                        error=error,
                    )
                    retried += 1
        return EmailDrainResult(
            sent_count=sent,
            retried_count=retried,
            abandoned_count=abandoned,
        )

    def abandoned_backlog(self) -> int:
        """How many people were never told something. Read on its own, because the
        drain is what fails during an outage and a figure it returned would read
        zero for exactly as long as the outage lasted."""
        with self.database.session() as session:
            return EmailOutboxRepository(session).abandoned_total()

    def redact(self, *, now: datetime | None = None, limit: int = 1_000) -> int:
        """Empty the bodies of messages already sent, keeping their delivery record."""
        current = now or utc_now()
        with self.database.session() as session:
            return EmailOutboxRepository(session).redact(
                sent_before=current - SENT_RETENTION,
                limit=limit,
                now=current,
            )

    def _deliver(self, sender: EmailSender, item: ClaimedEmail) -> tuple[_Verdict, str, str]:
        try:
            provider_message_id = sender.send(item.message)
        except InvalidInputError as exc:
            # The provider named something about this message it will refuse
            # identically forever, so retrying spends attempts to learn nothing.
            return _Verdict.Abandon, str(exc), ""
        except Exception as exc:
            if item.attempts >= MAX_ATTEMPTS:
                return _Verdict.Abandon, str(exc), ""
            return _Verdict.Retry, str(exc), ""
        return _Verdict.Sent, "", provider_message_id

    def _release(
        self,
        claimed: tuple[ClaimedEmail, ...],
        *,
        claim_token: str,
        now: datetime,
        error: str,
    ) -> None:
        with self.database.session() as session:
            repository = EmailOutboxRepository(session)
            for item in claimed:
                repository.mark_failed(
                    message_id=item.id,
                    claim_token=claim_token,
                    now=now,
                    next_attempt_at=next_attempt_at(item.attempts, now=now),
                    error=error,
                )


def next_attempt_at(attempts: int, *, now: datetime) -> datetime:
    """Exponential backoff, capped, so a provider outage is not also a stampede."""
    exponent = max(attempts - 1, 0)
    delay = min(_RETRY_BASE * (2**exponent), _RETRY_CAP)
    return now + delay


__all__ = [
    "CLAIM_TTL",
    "MAX_ATTEMPTS",
    "SENT_RETENTION",
    "EmailDrainResult",
    "EmailOutboxDrain",
    "enqueue_email",
    "next_attempt_at",
]
