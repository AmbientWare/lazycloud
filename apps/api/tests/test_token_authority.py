from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
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
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """The credential a person creates reaches every workspace their account holds.

    Which workspace it acts on is decided per request from membership, so the row
    carries an account and no workspace. A workspace on the row would pin it to one,
    and nothing here lets the caller ask for that.
    """
    admin_token, admin_record = administrator_credential(
        isolated_services.context, "token-authority"
    )
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
    owner_token, _owner = administrator_credential(isolated_services.context, "token-owner")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    created = TokenCreateResponse.model_validate_json(
        client.post(
            "/api/v1/tokens",
            headers=_auth(owner_token),
            json={"name": "owned-token"},
        ).content
    )

    stranger = UserService(isolated_services.context).create(
        display_name="token-stranger",
    )
    stranger_token, _stranger_record = AuthService(isolated_services.context).create_account_token(
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
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """Every credential the account holds is reachable by following the cursor.

    A row no page ever reaches is a credential its owner cannot revoke, and tokens
    minted in the same instant share a timestamp — which is why the cursor carries
    the identifier tie-break the ordering does.
    """
    owner_token, _owner = administrator_credential(isolated_services.context, "token-pager")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
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
    assert minted <= set(listed)


def test_token_cannot_revoke_its_own_record(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """A credential cannot end itself, so a mistake cannot lock the caller out."""
    method, suffix, detail = "POST", "/revoke", "cannot revoke the authenticating token"
    _admin_token, admin_record = administrator_credential(
        isolated_services.context, "self-mutation"
    )
    own_token, own_record = AuthService(isolated_services.context).create_account_token(
        admin_record.user_id,
        "self-mutation-token",
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.request(
        method,
        f"/api/v1/tokens/{own_record.id}{suffix}",
        headers=_auth(own_token),
    )

    _assert_error(response, 409, detail)
    listed = client.get("/api/v1/tokens", headers=_auth(own_token))
    assert listed.status_code == 200
    persisted = next(
        item
        for item in TokenListResponse.model_validate_json(listed.content).data
        if item.id == own_record.id
    )
    assert persisted.status is TokenStatus.Active


def test_only_an_administrator_may_mint_a_token_for_another_account(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """Minting on someone's behalf is what gives an identity-less account its first
    credential, so it has to stay an administrator's decision and has to produce a
    token naming the target rather than the administrator who ran it.
    """
    admin_token, admin_record = administrator_credential(isolated_services.context, "minting-admin")
    target = UserService(isolated_services.context).create(display_name="automation")
    auth = AuthService(isolated_services.context)
    member_token, _member_record = auth.create_account_token(
        UserService(isolated_services.context).create(display_name="ordinary").id,
        "member-cli",
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

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
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """Role is what platform authorization reads, so granting it stays an
    administrator's act. A member who could set their own would be one request away
    from reaching every workspace on the platform.
    """
    users = UserService(isolated_services.context)
    member = users.create(display_name="ordinary")
    member_token, _record = AuthService(isolated_services.context).create_account_token(
        member.id,
        "member-cli",
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    refused = client.put(
        f"/api/v1/users/{member.id}/role",
        headers=_auth(member_token),
        json={"role": "administrator"},
    )
    assert refused.status_code == 403
    assert users.get(member.id).role is PlatformRole.Member

    admin_token, _admin_record = administrator_credential(
        isolated_services.context, "promoting-admin"
    )
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
