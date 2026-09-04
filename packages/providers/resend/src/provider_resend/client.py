from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from shared.email import EmailMessage
from shared.errors import InvalidInputError, UpstreamUnavailableError

API_BASE_URL = "https://api.resend.com"


class _SendResult(BaseModel):
    """The part of an accepted send this platform keeps: the id events name."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""


class _ErrorBody(BaseModel):
    """The part of a Resend refusal worth repeating to an operator.

    `extra="ignore"` because Resend owns this shape and may add to it.
    """

    model_config = ConfigDict(extra="ignore")

    message: str = ""
    name: str = ""


@dataclass(slots=True)
class ResendEmailSender:
    """Deliver messages through Resend's send endpoint.

    The client carries the credential. Nothing here reads it back, and a
    refusal is reported with Resend's own words and never with the header
    that was sent.
    """

    client: httpx.Client
    from_address: str

    def send(self, message: EmailMessage) -> str:
        body: dict[str, str | list[str]] = {
            "from": self.from_address,
            "to": [message.to],
            "subject": message.subject,
            "html": message.html,
            "text": message.text,
        }
        try:
            response = self.client.post("/emails", json=body)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"Resend could not be reached: {exc}") from exc
        if not response.is_success:
            _raise_refusal(response)
        # Accepted, and the id is how a delivery event is matched to this
        # message later. An empty one has to be caught here rather than defaulted
        # through, or the message is sent and permanently unattributable.
        try:
            accepted = _SendResult.model_validate_json(response.content)
        except ValidationError as exc:
            raise UpstreamUnavailableError(
                "Resend accepted the message but its answer was unreadable"
            ) from exc
        if not accepted.id:
            raise UpstreamUnavailableError("Resend accepted the message but named no id for it")
        return accepted.id


def _raise_refusal(response: httpx.Response) -> None:
    """Say whether resending could ever work, because that is what the caller does next.

    A malformed recipient is ours to fix and will be refused identically forever.
    A rate limit, a rejected key and an unverified sending domain are not the
    caller's doing and are fixed by waiting or by an operator, so they stay
    retryable: reporting them as terminal would tell an administrator their
    invitation can never be sent when a rotation is all it needs.
    """
    status_code = response.status_code
    detail = _detail(response)
    message = f"Resend refused the message ({status_code}): {detail}"
    if status_code in {
        httpx.codes.TOO_MANY_REQUESTS,
        httpx.codes.UNAUTHORIZED,
        httpx.codes.FORBIDDEN,
    }:
        raise UpstreamUnavailableError(message)
    if httpx.codes.BAD_REQUEST <= status_code < httpx.codes.INTERNAL_SERVER_ERROR:
        raise InvalidInputError(message)
    raise UpstreamUnavailableError(message)


def _detail(response: httpx.Response) -> str:
    try:
        body = _ErrorBody.model_validate_json(response.content)
    except ValidationError:
        return response.text[:200] or "no detail"
    return body.message or body.name or "no detail"


def build_client(*, api_key: str, timeout_seconds: float) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_seconds,
    )


__all__ = ["API_BASE_URL", "ResendEmailSender", "build_client"]
