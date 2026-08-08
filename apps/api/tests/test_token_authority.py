from __future__ import annotations

from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from httpx2 import Response
from identity.auth import AuthService
from identity.users import UserService
from shared.http.errors import ErrorResponse
from shared.http.system import TokenCreateResponse, TokenListResponse
from shared.identity import TokenKind, TokenStatus
from tests.service_fixtures import administrator_credential


def test_a_minted_token_names_the_account_rather_than_a_workspace(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """The credential a person creates reaches every workspace their account holds.

    Which workspace it acts on is decided per request from membership, so the row
    carries an account and no workspace. A workspace on the row would pin it to one,
    and nothing here lets the caller ask for that.
    """
    admin_token, admin_record = administrator_credential(isolated_services, "token-authority")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.post(
        "/api/v1/tokens",
        headers=_auth(admin_token),
        json={"name": "account-token"},
    )

    assert response.status_code == 201
    created = TokenCreateResponse.model_validate_json(response.content)
    assert created.record.kind is TokenKind.User
    assert created.record.user_id == admin_record.user_id
    assert created.record.workspace_id == ""


def test_one_account_cannot_see_or_revoke_another_account_s_tokens(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """Tokens are listed and revoked through the account that owns them, only.

    The credential reaches every workspace its account belongs to, so reaching one
    from another account would hand over that whole account rather than one workspace.
    """
    owner_token, _owner = administrator_credential(isolated_services, "token-owner")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    created = TokenCreateResponse.model_validate_json(
        client.post(
            "/api/v1/tokens",
            headers=_auth(owner_token),
            json={"name": "owned-token"},
        ).content
    )

    stranger = UserService(isolated_services.context).create(
        username="token-stranger",
        password="token-stranger-password",
    )
    stranger_token, _stranger_record = AuthService(isolated_services.context).create_account_token(
        stranger.id,
        "stranger",
    )

    listed = client.get("/api/v1/tokens", headers=_auth(stranger_token))
    assert listed.status_code == 200
    assert created.record.id not in {
        item.id for item in TokenListResponse.model_validate_json(listed.content).tokens
    }

    revoked = client.post(
        f"/api/v1/tokens/{created.record.id}/revoke",
        headers=_auth(stranger_token),
    )
    assert revoked.status_code == 404

    still_active = client.get("/api/v1/tokens", headers=_auth(owner_token))
    persisted = next(
        item
        for item in TokenListResponse.model_validate_json(still_active.content).tokens
        if item.id == created.record.id
    )
    assert persisted.status is TokenStatus.Active


@pytest.mark.parametrize(
    ("method", "suffix", "detail"),
    [
        ("POST", "/revoke", "cannot revoke the authenticating token"),
        ("DELETE", "", "cannot delete the authenticating token"),
    ],
)
def test_token_cannot_mutate_its_own_record(
    method: str,
    suffix: str,
    detail: str,
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    admin_token, admin_record = administrator_credential(isolated_services, "self-mutation")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.request(
        method,
        f"/api/v1/tokens/{admin_record.id}{suffix}",
        headers=_auth(admin_token),
    )

    _assert_error(response, 409, detail)
    listed = client.get("/api/v1/tokens", headers=_auth(admin_token))
    assert listed.status_code == 200
    persisted = next(
        item
        for item in TokenListResponse.model_validate_json(listed.content).tokens
        if item.id == admin_record.id
    )
    assert persisted.status is TokenStatus.Active


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_error(response: Response, status_code: int, detail: str) -> None:
    assert response.status_code == status_code
    assert ErrorResponse.model_validate_json(response.content).detail == detail
