from __future__ import annotations

from api.server.services import ApiServices
from fastapi.testclient import TestClient
from httpx2 import Response
from identity.auth import AuthService
from identity.users import UserService
from shared.http.errors import ErrorResponse
from shared.http.system import TokenCreateResponse, TokenListResponse
from shared.identity import PlatformRole, TokenKind, TokenStatus
from tests.workspaces import administrator_credential


def test_a_minted_token_names_the_account_rather_than_a_workspace(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    admin_token, admin_record = administrator_credential(services.context, "token-authority")

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
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    owner_token, _owner = administrator_credential(services.context, "token-owner")
    created = TokenCreateResponse.model_validate_json(
        client.post(
            "/api/v1/tokens",
            headers=_auth(owner_token),
            json={"name": "owned-token"},
        ).content
    )

    stranger = UserService(services.context).create(
        display_name="token-stranger",
    )
    stranger_token, _stranger_record = AuthService(services.context).create_account_token(
        stranger.id,
        "stranger",
    )

    listed = client.get("/api/v1/tokens", headers=_auth(stranger_token))
    assert listed.status_code == 200
    assert created.record.id not in {
        item.id for item in TokenListResponse.model_validate_json(listed.content).data
    }

    revoked = client.post(
        f"/api/v1/tokens/{created.record.id}/revoke",
        headers=_auth(stranger_token),
    )
    assert revoked.status_code == 404

    still_active = client.get("/api/v1/tokens", headers=_auth(owner_token))
    persisted = next(
        item
        for item in TokenListResponse.model_validate_json(still_active.content).data
        if item.id == created.record.id
    )
    assert persisted.status is TokenStatus.Active


def test_the_account_token_list_pages_through_a_server_cursor(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    owner_token, _owner = administrator_credential(services.context, "token-pager")
    minted = {
        TokenCreateResponse.model_validate_json(
            client.post(
                "/api/v1/tokens",
                headers=_auth(owner_token),
                json={"name": f"paged-{index}"},
            ).content
        ).record.id
        for index in range(5)
    }

    listed: list[str] = []
    cursor = ""
    for _ in range(len(minted) + 1):
        response = client.get(
            f"/api/v1/tokens?limit=2&cursor={cursor}",
            headers=_auth(owner_token),
        )
        page = TokenListResponse.model_validate_json(response.content)
        listed.extend(item.id for item in page.data)
        cursor = page.next
        if not cursor:
            break

    assert cursor == ""
    assert len(listed) == len(set(listed))
    assert set(listed) == minted


def test_token_cannot_revoke_its_own_record(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    _admin_token, admin_record = administrator_credential(services.context, "self-mutation")
    own_token, own_record = AuthService(services.context).create_account_token(
        admin_record.user_id,
        "self-mutation-token",
    )
    response = client.post(
        f"/api/v1/tokens/{own_record.id}/revoke",
        headers=_auth(own_token),
    )

    _assert_error(response, 409, "cannot revoke the authenticating token")
    listed = client.get("/api/v1/tokens", headers=_auth(own_token))
    assert listed.status_code == 200
    persisted = next(
        item
        for item in TokenListResponse.model_validate_json(listed.content).data
        if item.id == own_record.id
    )
    assert persisted.status is TokenStatus.Active


def test_only_an_administrator_may_mint_a_token_for_another_account(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    admin_token, admin_record = administrator_credential(services.context, "minting-admin")
    target = UserService(services.context).create(display_name="automation")
    auth = AuthService(services.context)
    member_token, _member_record = auth.create_account_token(
        UserService(services.context).create(display_name="ordinary").id,
        "member-cli",
    )

    refused = client.post(
        f"/api/v1/users/{target.id}/tokens",
        headers=_auth(member_token),
        json={"name": "stolen"},
    )
    assert refused.status_code == 403

    granted = client.post(
        f"/api/v1/users/{target.id}/tokens",
        headers=_auth(admin_token),
        json={"name": "automation-cli"},
    )
    assert granted.status_code == 201, granted.text
    created = TokenCreateResponse.model_validate_json(granted.content)
    assert created.record.user_id == target.id
    assert created.record.user_id != admin_record.user_id
    assert created.record.workspace_id == ""


def test_a_member_cannot_promote_themselves_to_administrator(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    users = UserService(services.context)
    member = users.create(display_name="ordinary")
    member_token, _record = AuthService(services.context).create_account_token(
        member.id,
        "member-cli",
    )

    refused = client.put(
        f"/api/v1/users/{member.id}/role",
        headers=_auth(member_token),
        json={"role": "administrator"},
    )
    assert refused.status_code == 403
    assert users.get(member.id).role is PlatformRole.Member

    admin_token, _admin_record = administrator_credential(services.context, "promoting-admin")
    granted = client.put(
        f"/api/v1/users/{member.id}/role",
        headers=_auth(admin_token),
        json={"role": "administrator"},
    )
    assert granted.status_code == 200, granted.text
    assert users.get(member.id).role is PlatformRole.Administrator


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_error(response: Response, status_code: int, detail: str) -> None:
    assert response.status_code == status_code
    assert ErrorResponse.model_validate_json(response.content).detail == detail
