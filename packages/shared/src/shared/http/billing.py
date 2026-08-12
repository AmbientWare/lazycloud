from __future__ import annotations

from pydantic import Field

from shared.http.base import HttpModel


class BillingHostedSessionRequest(HttpModel):
    """Where to send the customer back to when they are done at the provider.

    Supplied by the caller rather than configured here because the dashboard
    knows which page the person was on, and returning everyone to one fixed
    location would lose that. Both are checked against the platform's own origin
    before they are used — an open redirect on a page that follows a card being
    saved is exactly the one worth abusing.
    """

    return_url: str = Field(min_length=1, max_length=2048)
    cancel_url: str = Field(default="", max_length=2048)
    """Where to return on abandoning the page. Falls back to `return_url`, which
    is the same page and correct: a customer who did not save a card should land
    where a customer who did lands, and find out there that nothing changed."""


class BillingHostedSessionResponse(HttpModel):
    """The page to send the customer to.

    A URL and nothing else. Card details are collected, stored, and shown by the
    payment provider, and never reach this platform — which is what keeps it
    outside the scope of cardholder data rules.
    """

    url: str = Field(min_length=1, max_length=2048)


__all__ = ["BillingHostedSessionRequest", "BillingHostedSessionResponse"]
