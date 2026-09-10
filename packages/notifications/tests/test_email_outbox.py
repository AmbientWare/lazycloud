from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from itertools import chain, repeat

from database.context import ServiceContext
from database.repositories.email_outbox import EmailOutboxRepository
from database.tables.email_outbox import EmailOutboxTable
from shared.email import EmailDeliveryState, EmailMessage
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.timestamps import utc_now
from sqlalchemy import select

from notifications import (
    MAX_ATTEMPTS,
    DeliveryReport,
    EmailOutboxDrain,
    discard_queued_email,
    enqueue_email,
    record_delivery,
)


@dataclass(slots=True)
class _Sender:
    """A provider that answers however the test needs it to."""

    error: Exception | None = None
    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append(message)
        return f"provider-{len(self.sent)}"


def _message(to: str = "someone@example.test") -> EmailMessage:
    return EmailMessage(to=to, subject="subject", html="<p>body</p>", text="body")


def _queue(context: ServiceContext, message: EmailMessage) -> str:
    with context.database.session() as session:
        return enqueue_email(session, message)


def _queued_ids(context: ServiceContext) -> list[str]:
    with context.database.session() as session:
        rows = session.execute(
            select(EmailOutboxTable.id).order_by(EmailOutboxTable.created_at)
        ).scalars()
        return [str(row) for row in rows]


def _delivery(context: ServiceContext, message_id: str) -> EmailDeliveryState:
    with context.database.session() as session:
        return EmailOutboxRepository(session).delivery_states([message_id])[message_id]


def _status(context: ServiceContext, message_id: str) -> str:
    with context.database.session() as session:
        state = EmailOutboxRepository(session).get_status(message_id)
    assert state is not None
    return state[0]


def test_a_queued_message_is_delivered_once_and_then_pruned(
    service_context: ServiceContext,
) -> None:
    """The queue is the durable half. The drain is the part that talks to anyone.

    Redaction matters as much as sending here. A sent row still holds the
    rendered body, and an invitation's body holds a working link, so the body is
    emptied while the row that says what happened is kept.
    """
    sender = _Sender()
    message_id = _queue(service_context, _message())
    drain = EmailOutboxDrain(database=service_context.database, sender_factory=lambda: sender)

    result = drain.drain()

    assert (result.sent_count, result.retried_count, result.abandoned_count) == (1, 0, 0)
    assert [item.to for item in sender.sent] == ["someone@example.test"]
    assert _status(service_context, message_id) == "sent"
    assert drain.drain().sent_count == 0

    # The body goes once the mail carrying it has gone. The row stays, because
    # it records what became of the message.
    assert drain.redact(now=utc_now() + timedelta(days=30)) == 1
    assert _status(service_context, message_id) == "sent"
    with service_context.database.session() as session:
        row = session.get(EmailOutboxTable, message_id)
        assert row is not None
        assert (row.html_body, row.text_body) == ("", "")


def test_a_refusal_that_a_later_attempt_could_fix_is_retried(
    service_context: ServiceContext,
) -> None:
    message_id = _queue(service_context, _message())
    sender = _Sender(error=UpstreamUnavailableError("the provider is unreachable"))
    drain = EmailOutboxDrain(database=service_context.database, sender_factory=lambda: sender)

    result = drain.drain()

    assert (result.sent_count, result.retried_count, result.abandoned_count) == (0, 1, 0)
    assert _status(service_context, message_id) == "pending"


def test_a_refusal_no_attempt_can_fix_is_abandoned_at_once(
    service_context: ServiceContext,
) -> None:
    """A malformed recipient is refused identically forever.

    Spending eight attempts on it would delay every message queued behind it to
    learn nothing, and would hide the one failure an operator has to see.
    """
    message_id = _queue(service_context, _message(to="not-an-address"))
    sender = _Sender(error=InvalidInputError("Resend refused the message (422): invalid to"))
    drain = EmailOutboxDrain(database=service_context.database, sender_factory=lambda: sender)

    result = drain.drain()

    assert (result.sent_count, result.retried_count, result.abandoned_count) == (0, 0, 1)
    assert _status(service_context, message_id) == "abandoned"
    assert drain.abandoned_backlog() == 1


def test_a_message_that_never_gets_through_is_given_up_on(
    service_context: ServiceContext,
) -> None:
    """Attempts are finite, so a queue cannot fill with messages nobody will read.

    Each sweep is run at a later clock rather than by resetting the row, so the
    backoff is what decides when the next attempt is due, exactly as it would in
    the scheduler.
    """
    message_id = _queue(service_context, _message())
    sender = _Sender(error=UpstreamUnavailableError("still unreachable"))
    drain = EmailOutboxDrain(database=service_context.database, sender_factory=lambda: sender)

    moment = utc_now()
    for _sweep in range(MAX_ATTEMPTS + 1):
        drain.drain(now=moment)
        moment += timedelta(minutes=20)

    assert _status(service_context, message_id) == "abandoned"
    assert drain.abandoned_backlog() == 1


def test_a_slow_provider_cannot_hold_up_the_rest_of_the_sweep(
    service_context: ServiceContext,
) -> None:
    """The sweep shares a housekeeping pass with billing and domain work.

    Sends are serial and each can take the provider's whole timeout, so a batch
    bounds the count and not the time. What is left keeps its claim and the next
    sweep takes it, so bounding the clock delays messages rather than losing any.
    """
    message_ids = [
        _queue(service_context, _message(to=f"someone{index}@example.test")) for index in range(4)
    ]
    sender = _Sender()
    # Reads the clock once to set the deadline, then before each message after
    # the first. The second read is already past it.
    readings = chain([0.0], repeat(99.0))
    drain = EmailOutboxDrain(
        database=service_context.database,
        sender_factory=lambda: sender,
        sweep_budget_seconds=1.0,
        monotonic=lambda: next(readings),
    )

    result = drain.drain()

    assert result.sent_count == 1
    assert len(sender.sent) == 1
    # The rest are still claimed rather than settled, so no attempt was wasted
    # on them and the next sweep takes them once the claim ages out.
    assert sorted(_status(service_context, message_id) for message_id in message_ids) == [
        "sending",
        "sending",
        "sending",
        "sent",
    ]


def test_a_deployment_with_no_email_provider_keeps_its_messages(
    service_context: ServiceContext,
) -> None:
    """No credential is a misconfiguration, not a reason to lose the message.

    The rows go back to pending so the sweep after somebody sets the variable
    delivers what was queued while it was missing.
    """

    def refuse() -> _Sender:
        raise RuntimeError("LAZYCLOUD_RESEND_API_KEY is not set")

    message_id = _queue(service_context, _message())
    drain = EmailOutboxDrain(database=service_context.database, sender_factory=refuse)

    for _sweep in range(MAX_ATTEMPTS + 4):
        drain.drain()

    assert _status(service_context, message_id) == "pending"
    # The claim charges an attempt and nothing was tried, so it is given back.
    # Without the refund the budget drains while the deployment sits
    # misconfigured, and the first real provider hiccup afterwards is the one
    # that abandons the message.
    with service_context.database.session() as session:
        state = EmailOutboxRepository(session).get_status(message_id)
    assert state is not None
    assert state[1] == 0

    # Once somebody sets the key, the message goes with its budget intact. Run
    # past the backoff the last failed sweep set, which is what a scheduler
    # ticking every thirty seconds does anyway.
    sender = _Sender()
    EmailOutboxDrain(database=service_context.database, sender_factory=lambda: sender).drain(
        now=utc_now() + timedelta(minutes=5)
    )
    assert [item.to for item in sender.sent] == ["someone@example.test"]


def test_a_delivery_report_lands_on_the_message_it_names(
    service_context: ServiceContext,
) -> None:
    """Accepting a message and delivering it are different events minutes apart.

    Without this the platform can only say it handed the message over, and an
    administrator asking why nobody answered has nothing to go on. Reports are
    ordered on the event's own timestamp, because they are not promised in order
    and a late `sent` must not erase a bounce that already landed.
    """
    _queue(service_context, _message())
    sender = _Sender()
    drain = EmailOutboxDrain(database=service_context.database, sender_factory=lambda: sender)
    drain.drain()
    message_id = _queued_ids(service_context)[0]
    assert _delivery(service_context, message_id) is EmailDeliveryState.Sent

    bounced_at = utc_now()
    with service_context.database.session() as session:
        assert record_delivery(
            session,
            DeliveryReport(
                provider_message_id="provider-1",
                state=EmailDeliveryState.Bounced,
                occurred_at=bounced_at,
                detail="mailbox does not exist",
            ),
        )
    assert _delivery(service_context, message_id) is EmailDeliveryState.Bounced

    with service_context.database.session() as session:
        record_delivery(
            session,
            DeliveryReport(
                provider_message_id="provider-1",
                state=EmailDeliveryState.Sent,
                occurred_at=bounced_at - timedelta(minutes=5),
            ),
        )
    assert _delivery(service_context, message_id) is EmailDeliveryState.Bounced


def test_a_report_for_a_message_we_no_longer_hold_is_not_an_error(
    service_context: ServiceContext,
) -> None:
    """A provider keeps its history longer than this platform keeps rows."""
    with service_context.database.session() as session:
        assert not record_delivery(
            session,
            DeliveryReport(
                provider_message_id="nothing-here",
                state=EmailDeliveryState.Delivered,
                occurred_at=utc_now(),
            ),
        )


def test_a_message_nothing_should_send_is_stopped_while_it_still_can_be(
    service_context: ServiceContext,
) -> None:
    """Resending an invitation kills the link the queued message carries.

    Letting that message go would deliver two invitations where the older one
    answers 404, which reads as the platform being broken rather than as an
    offer that moved.
    """
    stale = _queue(service_context, _message())
    with service_context.database.session() as session:
        assert discard_queued_email(session, stale)

    sender = _Sender()
    EmailOutboxDrain(database=service_context.database, sender_factory=lambda: sender).drain()

    assert sender.sent == []
    assert _status(service_context, stale) == "abandoned"


def test_an_abandoned_body_is_emptied_like_a_sent_one(
    service_context: ServiceContext,
) -> None:
    """Nobody is going to deliver it, and it still holds a working link."""
    message_id = _queue(service_context, _message())
    drain = EmailOutboxDrain(
        database=service_context.database,
        sender_factory=lambda: _Sender(error=InvalidInputError("refused for good")),
    )
    drain.drain()
    assert _status(service_context, message_id) == "abandoned"

    assert drain.redact(now=utc_now() + timedelta(days=30)) == 1
    with service_context.database.session() as session:
        row = session.get(EmailOutboxTable, message_id)
        assert row is not None
        assert (row.html_body, row.text_body) == ("", "")
