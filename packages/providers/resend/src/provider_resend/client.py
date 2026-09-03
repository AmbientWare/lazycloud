from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from shared.email import EmailMessage
from shared.errors import UpstreamUnavailableError

API_BASE_URL = "https://api.resend.com"


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

    def send(self, message: EmailMessage) -> None:
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
        if response.is_success:
            return
        raise UpstreamUnavailableError(
            f"Resend refused the message ({response.status_code}): {_refusal(response)}"
        )


def _refusal(response: httpx.Response) -> str:
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
