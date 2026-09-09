from __future__ import annotations

from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import TokenIssuer
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.identity import TokenKind
from tests.domain_fixtures import workspace_owner_user_id
from tests.service_fixtures import administrator_credential


@pytest.fixture
def stripe_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential the billing services capture when they are built.

    Requested before `isolated_services` so it is in the environment by the time
    the service graph reads it. Never used to call anything: the account under
    test already holds its provider ids, so the path refuses on the card before
    it would reach Stripe. The credential is resolved first on purpose — a
    deployment missing it must fail by name rather than only for the accounts
    that happen to need provisioning.
    """

    monkeypatch.setenv("LAZYCLOUD_STRIPE_API_KEY", "sk_test_unused_by_this_path")


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


def test_subscribing_without_a_card_answers_402(
    stripe_key: None,
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """The status is the contract, not the prose beside it.

    A `402` is what tells the dashboard this is a payment problem the customer
    can fix, rather than a platform fault they should retry into. The panel
    renders the server's message verbatim, so the words reach them either way —
    but a `500` would send them to a status page and a `409` would read as
    somebody else's change already in flight.
    """

    # An administrator holds no membership by design, and billing resolves the
    # workspace an account owns — so this run needs an owner, which is what the
    # default workspace already has.
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    owner_user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    issuer = TokenIssuer(isolated_services.context)
    with isolated_services.context.database.session() as session:
        raw_token, _ = issuer.issue_for_user(
            session, "billing-subscribe-owner", user_id=owner_user_id, kind=TokenKind.User
        )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    refused = client.post(
        "/api/v1/billing/subscription",
        json={
            "plan": BillingPlanId.Team.value,
            "terms_version": SubscriptionTermsVersion.Team.value,
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )

    assert refused.status_code == 402
    body = refused.json()
    assert body["code"] == "payment_required"
