from __future__ import annotations

from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import TokenIssuer
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.identity import TokenKind
from tests.workspaces import workspace_owner_user_id


@pytest.fixture
def stripe_key(monkeypatch: pytest.MonkeyPatch) -> None:

    # Resolved while building the billing services, before card admission.
    monkeypatch.setenv("LAZYCLOUD_STRIPE_API_KEY", "sk_test_unused_by_this_path")


def test_a_billing_return_address_must_be_on_this_platform(
    api_runtime: tuple[ApiServices, TestClient],
    api_client: TestClient,
) -> None:

    services, _ = api_runtime
    client = api_client
    own_origin = services.gateway_settings.public_http_url

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
    )
    assert refused_cancel.status_code == 400


def test_subscribing_without_a_card_answers_402(
    stripe_key: None,
    isolated_services: ApiServices,
) -> None:

    # An administrator holds no membership by design, and billing resolves the
    # workspace an account owns — so this run needs an owner, which is what the
    # default workspace already has.
    with ExitStack() as client_stack:
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
