from __future__ import annotations

from typing import Protocol

from shared.contracts import ContractModel


class EmailMessage(ContractModel):
    """One transactional message, complete before it reaches a provider.

    Both bodies are required. A client that cannot render HTML shows the text
    body, and a message with only HTML reads as empty there.
    """

    to: str
    subject: str
    html: str
    text: str


class EmailSender(Protocol):
    """Deliver one message, or raise naming why it could not be delivered."""

    def send(self, message: EmailMessage) -> None: ...


__all__ = ["EmailMessage", "EmailSender"]
