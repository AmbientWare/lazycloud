from __future__ import annotations

from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from httpx2 import Response
from identity.auth import AuthService
from shared.http.errors import ErrorResponse
from shared.http.system import TokenCreateResponse, TokenListResponse
from shared.identity import AuthScope, TokenKind, TokenStatus
from tests.service_fixtures import administrator_credential


def test_workspace_writer_can_issue_an_ordinary_workspace_token(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    auth = AuthService(isolated_services.context)
    creator_token, creator = auth.create_token(
        "workspace-writer",
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.post(
        "/api/v1/tokens",
        headers=_auth(creator_token),
        json={
            "name": "ordinary-workspace-token",
            "kind": TokenKind.Workspace.value,
            "workspace_id": creator.workspace_id,
        },
    )

    assert response.status_code == 201
    created = TokenCreateResponse.model_validate_json(response.content)
    assert created.record.kind is TokenKind.Workspace
    assert created.record.workspace_id == creator.workspace_id


@pytest.mark.parametrize(
    "requested_kind",
    [
        TokenKind.WorkspacePrimary,
        TokenKind.WorkspaceRestricted,
        TokenKind.Worker,
        TokenKind.WorkerPrivate,
        TokenKind.Machine,
    ],
)
def test_workspace_writer_cannot_issue_a_privileged_token_kind(
    requested_kind: TokenKind,
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    auth = AuthService(isolated_services.context)
    creator_token, creator = auth.create_token(
        "workspace-writer",
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.post(
        "/api/v1/tokens",
        headers=_auth(creator_token),
        json={
            "name": f"forbidden-{requested_kind.value}",
            "kind": requested_kind.value,
            "workspace_id": creator.workspace_id,
        },
    )

    _assert_error(response, 403, "admin token required to issue non-workspace tokens")
    assert [record.name for record in auth.list_workspace_tokens(creator.workspace_id)] == [
        creator.name
    ]


@pytest.mark.parametrize("requested_kind", [TokenKind.Admin, TokenKind.User, TokenKind.Session])
def test_no_one_can_mint_an_account_credential_from_the_workspace_token_route(
    requested_kind: TokenKind,
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """Not even an administrator, because the result would belong to nobody.

    These kinds mean "this names a person", and this route writes a row that names a
    workspace. The token that came out passed every administrator check while having
    no account to attribute, revoke, or disable it through.
    """
    admin_token, _record = administrator_credential(isolated_services, "token-authority-admin")
    auth = AuthService(isolated_services.context)
    workspace_id = ControlPlaneService(isolated_services.context).get_workspace("default").id
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.post(
        "/api/v1/tokens",
        headers=_auth(admin_token),
        json={
            "name": f"forbidden-{requested_kind.value}",
            "kind": requested_kind.value,
            "workspace_id": workspace_id,
        },
    )

    _assert_error(
        response,
        400,
        f"{requested_kind.value} credentials name an account, not a workspace; "
        f"they come from signing in or from the offline administrator bootstrap",
    )
    assert not [
        record
        for record in auth.list_workspace_tokens(workspace_id)
        if record.name.startswith("forbidden-")
    ]


@pytest.mark.parametrize(
    ("method", "suffix", "detail"),
    [
        ("POST", "/revoke", "cannot revoke the authenticating token"),
        ("POST", "/toggle", "cannot toggle the authenticating token"),
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
    auth = AuthService(isolated_services.context)
    raw_token, record = auth.create_token(
        "workspace-writer",
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.request(
        method,
        f"/api/v1/tokens/{record.id}{suffix}",
        headers=_auth(raw_token),
    )

    _assert_error(response, 409, detail)
    listed = client.get("/api/v1/tokens", headers=_auth(raw_token))
    assert listed.status_code == 200
    persisted = next(
        item
        for item in TokenListResponse.model_validate_json(listed.content).tokens
        if item.id == record.id
    )
    assert persisted.status is TokenStatus.Active


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_error(response: Response, status_code: int, detail: str) -> None:
    assert response.status_code == status_code
    assert ErrorResponse.model_validate_json(response.content).detail == detail
