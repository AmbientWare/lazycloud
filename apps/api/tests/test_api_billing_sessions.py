from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from tests.service_fixtures import administrator_credential


def test_a_billing_return_address_must_be_on_this_platform(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """The payment provider redirects the customer to whatever this hands it.

    An unchecked return address is an open redirect on the page that follows
    saving a card — the one worth abusing, because the person arriving at it has
    just been sent somewhere unfamiliar to do something with their money and is
    primed to believe the next page too.

    Refused before any provider call, so a rejected address cannot leave a
    customer registered at the provider on the strength of a request that was
    never going to be served.
    """

    raw_token, _ = administrator_credential(isolated_services, "billing-redirect")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}
    own_origin = isolated_services.gateway_settings.public_http_url

    scheme, _, host = own_origin.partition("://")
    other_scheme = "http" if scheme == "https" else "https"
    for elsewhere in (
        "https://lazycloud.example.evil/dashboard",
        # The platform's own host reached over the other scheme, which is a
        # different origin and is how a downgrade gets waved through.
        f"{other_scheme}://{host}/settings/billing",
        # Same host, somebody else's port.
        f"{scheme}://{host.partition(':')[0]}:9/settings/billing",
        "//evil.example/dashboard",
        "javascript:alert(1)",
    ):
        refused = client.post(
            "/api/v1/billing/card-session",
            json={"return_url": elsewhere},
            headers=headers,
        )
        assert refused.status_code == 400, elsewhere

    # And the cancel address is checked as well as the success one: a caller that
    # abandons the page is redirected by exactly the same mechanism.
    refused_cancel = client.post(
        "/api/v1/billing/card-session",
        json={
            "return_url": f"{own_origin}/settings/billing",
            "cancel_url": "https://evil.example/dashboard",
        },
        headers=headers,
    )
    assert refused_cancel.status_code == 400
