from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
from typing import Never

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.identity import DeviceAuthorizationRepository, WorkspaceMemberRepository
from database.tables.identity import DeviceAuthorizationTable
from database.types import DatabaseSession
from fastapi.testclient import TestClient
from identity.auth import AuthService, TokenIssuer
from identity.device_auth import (
    DEVICE_CODE_TTL_SECONDS,
    DeviceAuthorizationService,
)
from identity.users import UserService
from shared.identity import (
    DeviceAuthorizationStatus,
    TokenKind,
    UserRecord,
)
from shared.timestamps import utc_now
from sqlalchemy import update
from tests.workspaces import owned_workspace


def _signed_in_user(
    services: ApiServices,
    *,
    display_name: str = "operator",
    workspace: str = "default",
) -> tuple[UserRecord, dict[str, str]]:
    """A person who belongs to a workspace, and a credential that names them."""
    users = UserService(services.context)
    user = users.create(display_name=display_name)
    with services.context.database.session() as session:
        workspace_id = services.context.workspace(session, workspace).id
        WorkspaceMemberRepository(session).add(
            workspace_id=workspace_id,
            user_id=user.id,
        )
    issuer = TokenIssuer(services.context)
    with services.context.database.session() as session:
        raw_token, _ = issuer.issue_for_user(session, display_name, user_id=user.id)
    issuer.committed()
    return user, {"Authorization": f"Bearer {raw_token}"}


def test_device_login_flow_approves_and_mints_account_token(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        isolated_services.gateway_settings,
        "public_http_url",
        "https://control.example.com",
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    _user, headers = _signed_in_user(isolated_services)

    started = client.post("/auth/device", json={"client_name": "cli@laptop"})
    assert started.status_code == 201
    start = started.json()
    assert start["verification_uri"] == "https://control.example.com/activate"
    assert start["verification_uri_complete"] == (
        f"https://control.example.com/activate?code={start['user_code']}"
    )
    assert start["expires_in_seconds"] == DEVICE_CODE_TTL_SECONDS
    assert start["poll_interval_seconds"] >= 1

    pending = client.post("/auth/device/token", json={"device_code": start["device_code"]})
    assert pending.status_code == 200
    assert pending.json() == {"status": "pending", "token": ""}

    shown = client.get(f"/api/v1/device-codes/{start['user_code']}", headers=headers)
    assert shown.status_code == 200
    assert shown.json()["client_name"] == "cli@laptop"
    assert shown.json()["status"] == "pending"

    approved = client.post(
        f"/api/v1/device-codes/{start['user_code']}/approve",
        headers=headers,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    claimed = client.post("/auth/device/token", json={"device_code": start["device_code"]})
    assert claimed.status_code == 200
    claim = claimed.json()
    assert claim["status"] == "approved"
    assert claim["token"]

    # The minted token names the approving account and reaches its workspaces.
    workspaces = client.get(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {claim['token']}"},
    )
    assert workspaces.status_code == 200

    # The durable consumption marker rejects replay without losing audit state.
    replay = client.post("/auth/device/token", json={"device_code": start["device_code"]})
    assert replay.status_code == 409
    assert replay.json()["detail"] == "device code was already consumed"


def test_device_code_approval_requires_a_user_credential(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """A workspace-scoped automation token cannot hand out account-wide access.

    The credential the CLI claims reaches every workspace the approver belongs to, so
    approving is an act only a person can perform.
    """
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    auth = AuthService(isolated_services.context)
    other_workspace = owned_workspace(ControlPlaneService(isolated_services.context), "other")
    raw_other, _ = auth.create_token(
        "other-workspace",
        kind=TokenKind.Workspace,
        workspace_id=other_workspace.id,
    )

    start = client.post("/auth/device", json={"client_name": "cli"}).json()
    approve = client.post(
        f"/api/v1/device-codes/{start['user_code']}/approve",
        headers={"Authorization": f"Bearer {raw_other}"},
    )
    assert approve.status_code == 403

    unauthenticated = client.post(f"/api/v1/device-codes/{start['user_code']}/approve")
    assert unauthenticated.status_code == 401

    claim = client.post("/auth/device/token", json={"device_code": start["device_code"]})
    assert claim.json()["status"] == "pending"


def test_device_codes_expire_and_are_pruned(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    service = DeviceAuthorizationService(isolated_services.context)
    started = service.start(client_name="cli")

    future = utc_now() + timedelta(seconds=DEVICE_CODE_TTL_SECONDS + 1)
    record = service.get(started.record.user_code)
    assert record.status is DeviceAuthorizationStatus.Pending

    assert service.prune_expired(now=future) == 1
    assert service.prune_expired(now=future) == 0

    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    assert (
        client.post("/auth/device/token", json={"device_code": started.device_code}).status_code
        == 404
    )


def test_expired_device_code_claim_reports_expired_before_prune(
    service_context: ServiceContext,
) -> None:
    service = DeviceAuthorizationService(service_context)
    started = service.start(client_name="cli")

    with service_context.database.session() as session:
        repository = DeviceAuthorizationRepository(session)
        record = repository.by_user_code(started.record.user_code)
        assert record is not None
        session.execute(
            update(DeviceAuthorizationTable)
            .where(DeviceAuthorizationTable.id == record.id)
            .values(expires_at=utc_now() - timedelta(seconds=1))
        )

    claim = service.claim(started.device_code)
    assert claim.status is DeviceAuthorizationStatus.Expired
    assert claim.token == ""


def test_device_claim_rolls_back_consumption_when_token_insert_fails(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DeviceAuthorizationService(isolated_services.context)
    started = service.start(client_name="cli")
    user, _ = _signed_in_user(isolated_services)
    service.approve(started.record.user_code, user_id=user.id)

    original_issue = TokenIssuer.issue_for_user

    def fail_issue(
        self: TokenIssuer,
        session: DatabaseSession,
        name: str,
        *,
        user_id: str,
        kind: TokenKind = TokenKind.User,
        scopes: list[str] | None = None,
        expires_in_seconds: int | None = None,
        reusable: bool = True,
    ) -> Never:
        del (self, session, name, user_id, kind, scopes, expires_in_seconds, reusable)
        raise RuntimeError("token insert failed")

    monkeypatch.setattr(TokenIssuer, "issue_for_user", fail_issue)
    with pytest.raises(RuntimeError, match="token insert failed"):
        service.claim(started.device_code)

    monkeypatch.setattr(TokenIssuer, "issue_for_user", original_issue)
    claimed = service.claim(started.device_code)
    assert claimed.status is DeviceAuthorizationStatus.Approved
    assert claimed.token
