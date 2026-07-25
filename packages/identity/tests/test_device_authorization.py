from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
from typing import Never

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.identity import DeviceAuthorizationRepository
from database.tables.identity import DeviceAuthorizationTable
from database.types import DatabaseSession
from fastapi.testclient import TestClient
from identity.auth import AuthService, TokenIssuer
from identity.device_auth import (
    DEVICE_CODE_TTL_SECONDS,
    DeviceAuthorizationService,
)
from pydantic import JsonValue
from shared.identity import AuthTokenRecord, DeviceAuthorizationStatus, TokenKind
from shared.timestamps import utc_now
from sqlalchemy import update


def _admin_headers(services: ApiServices) -> dict[str, str]:
    raw_token, _ = AuthService(services.context).create_token(
        "admin",
        kind=TokenKind.Admin,
        workspace_id="default",
    )
    return {"Authorization": f"Bearer {raw_token}"}


def test_device_login_flow_approves_and_mints_workspace_token(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = _admin_headers(isolated_services)

    started = client.post("/auth/device", json={"client_name": "cli@laptop"})
    assert started.status_code == 201
    start = started.json()
    assert start["verification_uri"].endswith("/activate")
    assert start["verification_uri_complete"].endswith(f"/activate?code={start['user_code']}")
    assert start["expires_in_seconds"] == DEVICE_CODE_TTL_SECONDS
    assert start["poll_interval_seconds"] >= 1

    pending = client.post("/auth/device/token", json={"device_code": start["device_code"]})
    assert pending.status_code == 200
    assert pending.json() == {"status": "pending", "token": "", "workspace": ""}

    shown = client.get(f"/api/v1/device-codes/{start['user_code']}", headers=headers)
    assert shown.status_code == 200
    assert shown.json()["client_name"] == "cli@laptop"
    assert shown.json()["status"] == "pending"

    approved = client.post(
        f"/api/v1/device-codes/{start['user_code']}/approve",
        json={"workspace": "default"},
        headers=headers,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    claimed = client.post("/auth/device/token", json={"device_code": start["device_code"]})
    assert claimed.status_code == 200
    claim = claimed.json()
    assert claim["status"] == "approved"
    assert claim["workspace"] == "default"
    assert claim["token"]

    # The minted token is a workspace credential that works for API access.
    workspaces = client.get(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {claim['token']}"},
    )
    assert workspaces.status_code == 200

    # The durable consumption marker rejects replay without losing audit state.
    replay = client.post("/auth/device/token", json={"device_code": start["device_code"]})
    assert replay.status_code == 409
    assert replay.json()["detail"] == "device code was already consumed"


def test_device_code_approval_requires_workspace_write_access(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    auth = AuthService(isolated_services.context)
    other_workspace = ControlPlaneService(isolated_services.context).upsert_workspace("other")
    raw_other, _ = auth.create_token(
        "other-workspace",
        kind=TokenKind.Workspace,
        workspace_id=other_workspace.id,
    )

    start = client.post("/auth/device", json={"client_name": "cli"}).json()
    approve = client.post(
        f"/api/v1/device-codes/{start['user_code']}/approve",
        json={"workspace": "default"},
        headers={"Authorization": f"Bearer {raw_other}"},
    )
    assert approve.status_code == 403

    unauthenticated = client.post(
        f"/api/v1/device-codes/{start['user_code']}/approve",
        json={"workspace": "default"},
    )
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
    isolated_services: ApiServices,
) -> None:
    service = DeviceAuthorizationService(isolated_services.context)
    started = service.start(client_name="cli")

    with isolated_services.context.database.session() as session:
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
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.workspace(session).id
    service.approve(started.record.user_code, workspace_id=workspace_id)

    original_issue = TokenIssuer.issue

    def fail_issue(
        self: TokenIssuer,
        session: DatabaseSession,
        name: str,
        *,
        scopes: list[str] | None = None,
        expires_in_seconds: int | None = None,
        kind: TokenKind | str = TokenKind.Workspace,
        workspace_id: str = "default",
        worker_id: str = "",
        reusable: bool = True,
        audit_actor: AuthTokenRecord | None = None,
    ) -> Never:
        del (
            self,
            session,
            name,
            scopes,
            expires_in_seconds,
            kind,
            workspace_id,
            worker_id,
            reusable,
            audit_actor,
        )
        raise RuntimeError("token insert failed")

    monkeypatch.setattr(TokenIssuer, "issue", fail_issue)
    with pytest.raises(RuntimeError, match="token insert failed"):
        service.claim(started.device_code)

    monkeypatch.setattr(TokenIssuer, "issue", original_issue)
    claimed = service.claim(started.device_code)
    assert claimed.status is DeviceAuthorizationStatus.Approved
    assert claimed.token


def _device_start_payload() -> dict[str, JsonValue]:
    return {
        "device_code": "dc_secret",
        "user_code": "BCDF-GHJK",
        "verification_uri": "http://127.0.0.1:9000/activate",
        "verification_uri_complete": "http://127.0.0.1:9000/activate?code=BCDF-GHJK",
        "expires_in_seconds": 900,
        "poll_interval_seconds": 5,
    }
