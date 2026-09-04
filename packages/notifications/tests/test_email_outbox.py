from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from itertools import chain, repeat

from api.server.services import ApiServices
from database.repositories.email_outbox import EmailOutboxRepository
from database.tables.email_outbox import EmailOutboxTable
from shared.email import EmailMessage
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.timestamps import utc_now
from sqlalchemy import select

from notifications import MAX_ATTEMPTS, EmailOutboxDrain, enqueue_email


@dataclass(slots=True)
class _Sender:
    """A provider that answers however the test needs it to."""

    error: Exception | None = None
    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(message)


def _message(to: str = "someone@example.test") -> EmailMessage:
    return EmailMessage(to=to, subject="subject", html="<p>body</p>", text="body")


def _queue(services: ApiServices, message: EmailMessage) -> str:
    with services.context.database.session() as session:
        return enqueue_email(session, message)


def _queued_ids(services: ApiServices) -> list[str]:
    with services.context.database.session() as session:
        rows = session.execute(
            select(EmailOutboxTable.id).order_by(EmailOutboxTable.created_at)
        ).scalars()
        return [str(row) for row in rows]


def _status(services: ApiServices, message_id: str) -> str:
    with services.context.database.session() as session:
        state = EmailOutboxRepository(session).get_status(message_id)
    assert state is not None
    return state[0]


def test_a_queued_message_is_delivered_once_and_then_pruned(
    isolated_services: ApiServices,
) -> None:
    """The queue is the durable half; the drain is the part that talks to anyone.

    Pruning matters as much as sending here: a delivered row still holds the
    rendered body, and an invitation's body holds a working link.
    """
    sender = _Sender()
    message_id = _queue(isolated_services, _message())
    drain = EmailOutboxDrain(
        database=isolated_services.context.database, sender_factory=lambda: sender
    )

    result = drain.drain()

    assert (result.sent_count, result.retried_count, result.abandoned_count) == (1, 0, 0)
    assert [item.to for item in sender.sent] == ["someone@example.test"]
    assert _status(isolated_services, message_id) == "sent"
    assert drain.drain().sent_count == 0

    pruned = drain.prune(now=utc_now().replace(year=utc_now().year + 1))
    assert pruned == 1


def test_a_refusal_that_a_later_attempt_could_fix_is_retried(
    isolated_services: ApiServices,
) -> None:
    message_id = _queue(isolated_services, _message())
    sender = _Sender(error=UpstreamUnavailableError("the provider is unreachable"))
    drain = EmailOutboxDrain(
        database=isolated_services.context.database, sender_factory=lambda: sender
    )

    result = drain.drain()

    assert (result.sent_count, result.retried_count, result.abandoned_count) == (0, 1, 0)
    assert _status(isolated_services, message_id) == "pending"


def test_a_refusal_no_attempt_can_fix_is_abandoned_at_once(
    isolated_services: ApiServices,
) -> None:
    """A malformed recipient is refused identically forever.

    Spending eight attempts on it would delay every message queued behind it to
    learn nothing, and would hide the one failure an operator has to see.
    """
    message_id = _queue(isolated_services, _message(to="not-an-address"))
    sender = _Sender(error=InvalidInputError("Resend refused the message (422): invalid to"))
    drain = EmailOutboxDrain(
        database=isolated_services.context.database, sender_factory=lambda: sender
    )

    result = drain.drain()

    assert (result.sent_count, result.retried_count, result.abandoned_count) == (0, 0, 1)
    assert _status(isolated_services, message_id) == "abandoned"
    assert drain.abandoned_backlog() == 1


def test_a_message_that_never_gets_through_is_given_up_on(
    isolated_services: ApiServices,
) -> None:
    """Attempts are finite, so a queue cannot fill with messages nobody will read.

    Each sweep is run at a later clock rather than by resetting the row, so the
    backoff is what decides when the next attempt is due, exactly as it would in
    the scheduler.
    """
    message_id = _queue(isolated_services, _message())
    sender = _Sender(error=UpstreamUnavailableError("still unreachable"))
    drain = EmailOutboxDrain(
        database=isolated_services.context.database, sender_factory=lambda: sender
    )

    moment = utc_now()
    for _sweep in range(MAX_ATTEMPTS + 1):
        drain.drain(now=moment)
        moment += timedelta(minutes=20)

    assert _status(isolated_services, message_id) == "abandoned"
    assert drain.abandoned_backlog() == 1


def test_a_slow_provider_cannot_hold_up_the_rest_of_the_sweep(
    isolated_services: ApiServices,
) -> None:
    """The sweep shares a housekeeping pass with billing and domain work.

    Sends are serial and each can take the provider's whole timeout, so a batch
    bounds the count and not the time. What is left keeps its claim and the next
    sweep takes it, so bounding the clock delays messages rather than losing any.
    """
    for index in range(4):
        _queue(isolated_services, _message(to=f"someone{index}@example.test"))
    sender = _Sender()
    # Reads the clock once to set the deadline, then before each message after
    # the first. The second read is already past it.
    readings = chain([0.0], repeat(99.0))
    drain = EmailOutboxDrain(
        database=isolated_services.context.database,
        sender_factory=lambda: sender,
        sweep_budget_seconds=1.0,
        monotonic=lambda: next(readings),
    )

    result = drain.drain()

    assert result.sent_count == 1
    assert [item.to for item in sender.sent] == ["someone0@example.test"]
    # The rest are still claimed rather than settled, so no attempt was wasted
    # on them and the next sweep takes them once the claim ages out.
    assert _status(isolated_services, _queued_ids(isolated_services)[-1]) == "sending"


def test_a_deployment_with_no_email_provider_keeps_its_messages(
    isolated_services: ApiServices,
) -> None:
    """No credential is a misconfiguration, not a reason to lose the message.

    The rows go back to pending so the sweep after somebody sets the variable
    delivers what was queued while it was missing.
    """

    def refuse() -> _Sender:
        raise RuntimeError("LAZYCLOUD_RESEND_API_KEY is not set")

    message_id = _queue(isolated_services, _message())
    drain = EmailOutboxDrain(database=isolated_services.context.database, sender_factory=refuse)

    result = drain.drain()

    assert (result.sent_count, result.abandoned_count) == (0, 0)
    assert _status(isolated_services, message_id) == "pending"
