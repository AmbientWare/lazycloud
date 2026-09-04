from __future__ import annotations

from typing import Protocol

from shared.contracts import ContractModel
from shared.enums import StringEnum


class EmailMessage(ContractModel):
    """One transactional message, complete before it reaches a provider.

    Both bodies are required. A client that cannot render HTML shows the text
    body, and a message with only HTML reads as empty there.
    """

    to: str
    subject: str
    html: str
    text: str


class EmailDeliveryState(StringEnum):
    """What became of a message after the provider accepted it.

    Accepting is not delivering. A provider answers in milliseconds and the
    outcome arrives seconds or minutes later, so a message the platform sent
    successfully and one that reached somebody are different facts and this is
    the one worth showing a person.
    """

    Queued = "queued"
    """Written, not yet handed over."""

    Sent = "sent"
    """The provider accepted it. Nothing yet says anyone received it."""

    Delivered = "delivered"
    Bounced = "bounced"
    Complained = "complained"
    """Delivered, and the recipient marked it as spam."""

    Failed = "failed"
    """The platform gave up before the provider ever accepted it."""

    @property
    def reached_someone(self) -> bool:
        return self is EmailDeliveryState.Delivered

    @property
    def needs_attention(self) -> bool:
        """Whether somebody should do something about this message."""
        return self in {
            EmailDeliveryState.Bounced,
            EmailDeliveryState.Complained,
            EmailDeliveryState.Failed,
        }


class EmailSender(Protocol):
    """Deliver one message, returning the provider's id for it.

    The id is what a delivery event names later, so a sender that did not return
    one would leave every message unmatchable when the provider reports what
    happened to it.
    """

    def send(self, message: EmailMessage) -> str: ...


__all__ = ["EmailDeliveryState", "EmailMessage", "EmailSender"]
