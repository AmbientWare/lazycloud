from __future__ import annotations

import hashlib
import hmac

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from provider_stripe import StripeSettings

_SECRET = "whsec_endpoint_secret_for_the_api_test"
_BODY = b'{"id":"evt_api_1","type":"invoice.paid","data":{"object":{"id":"in_absent"}}}'


@pytest.fixture
def signed_client(isolated_services: ApiServices, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAZYCLOUD_STRIPE_WEBHOOK_SECRET", _SECRET)
    services = isolated_services
    object.__setattr__(services, "stripe_settings", StripeSettings())
    with TestClient(create_app(services)) as client:
        yield client


def _signature(body: bytes, secret: str, timestamp: int) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_the_payment_endpoint_refuses_anything_it_did_not_sign(
    signed_client: TestClient,
) -> None:
    """This route is reachable by anyone and changes what customers owe.

    It carries no bearer token — the provider holds none of ours — so the
    endpoint secret is the entire authorization. A delivery that arrives without
    a signature, or with one from another secret, is an anonymous request to mark
    an account paid or in arrears.
    """

    unsigned = signed_client.post("/webhooks/stripe", content=_BODY)
    assert unsigned.status_code == 400

    import time

    forged = signed_client.post(
        "/webhooks/stripe",
        content=_BODY,
        headers={"Stripe-Signature": _signature(_BODY, "whsec_not_ours", int(time.time()))},
    )
    assert forged.status_code == 400

    accepted = signed_client.post(
        "/webhooks/stripe",
        content=_BODY,
        headers={"Stripe-Signature": _signature(_BODY, _SECRET, int(time.time()))},
    )
    # Signed and acknowledged. The invoice belongs to no period here, which is a
    # delivery to ignore rather than a request to reject: the provider retries
    # anything that is not 2xx, and retrying will not make it ours.
    assert accepted.status_code == 204
