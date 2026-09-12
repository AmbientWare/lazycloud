from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.identity import WorkspaceMemberRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService, TokenIssuer
from identity.device_auth import (
    DEVICE_CODE_TTL_SECONDS,
    DeviceAuthorizationService,
)
from identity.users import UserService
from shared.http.device_auth import DeviceCodeCreateResponse
from shared.http.system import TokenListResponse
from shared.identity import (
    DeviceAuthorizationStatus,
    TokenKind,
    UserRecord,
)
from shared.timestamps import utc_now
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ExitStack() as client_stack:
        monkeypatch.setattr(
            isolated_services.gateway_settings,
            "public_http_url",
            "https://control.example.com",
        )
        client = client_stack.enter_context(TestClient(create_app(isolated_services)))
        user, headers = _signed_in_user(isolated_services)
        manual = client.post("/api/v1/tokens", headers=headers, json={"name": "cli@laptop"})
        assert manual.status_code == 201
        manual_id = manual.json()["record"]["id"]
        with isolated_services.context.database.session() as session:
            TokenIssuer(isolated_services.context).issue_for_user(
                session, "session", user_id=user.id, kind=TokenKind.Session
            )

        started = client.post("/auth/device", json={"client_name": "work-laptop"})
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
        assert shown.json()["client_name"] == "work-laptop"
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

        listed = client.get("/api/v1/tokens", headers=headers)
        assert listed.status_code == 200
        tokens = TokenListResponse.model_validate_json(listed.content).data
        device = next(item for item in tokens if item.name == "work-laptop")
        assert device.device_login
        assert not next(item for item in tokens if item.id == manual_id).device_login
        assert all(item.kind is TokenKind.User for item in tokens)

        filtered = client.get("/api/v1/tokens?include_device=false&limit=1", headers=headers)
        assert filtered.status_code == 200
        page = TokenListResponse.model_validate_json(filtered.content)
        assert [item.id for item in page.data] == [manual_id]
        assert page.next
        following = client.get(
            "/api/v1/tokens",
            params={"include_device": "false", "limit": 1, "cursor": page.next},
            headers=headers,
        )
        next_page = TokenListResponse.model_validate_json(following.content)
        assert [item.name for item in next_page.data] == ["operator"]
        assert not next_page.next

        revoked = client.post(f"/api/v1/tokens/{device.id}/revoke", headers=headers)
        assert revoked.status_code == 200
        assert (
            client.get(
                "/api/v1/workspaces", headers={"Authorization": f"Bearer {claim['token']}"}
            ).status_code
            == 401
        )

        # The durable consumption marker rejects replay without losing audit state.
        replay = client.post("/auth/device/token", json={"device_code": start["device_code"]})
        assert replay.status_code == 409
        assert replay.json()["detail"] == "device code was already consumed"


def test_device_code_approval_requires_a_user_credential(
    isolated_services: ApiServices,
) -> None:
    """A workspace-scoped automation token cannot hand out account-wide access.

    The credential the CLI claims reaches every workspace the approver belongs to, so
    approving is an act only a person can perform.
    """
    with ExitStack() as client_stack:
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


def test_device_polling_has_an_independent_bounded_budget(
    isolated_services: ApiServices,
) -> None:
    with TestClient(create_app(isolated_services)) as client:
        devices: list[DeviceCodeCreateResponse] = []
        for name in ("first-cli", "second-cli"):
            response = client.post("/auth/device", json={"client_name": name})
            assert response.status_code == 201
            devices.append(DeviceCodeCreateResponse.model_validate(response.json()))

        normal_polls = sum(60 // device.poll_interval_seconds + 1 for device in devices)
        for index in range(normal_polls):
            response = client.post(
                "/auth/device/token",
                json={"device_code": devices[index % 2].device_code},
            )
            assert response.status_code == 200
            assert response.json() == {"status": "pending", "token": ""}

        for _ in range(8):
            assert (
                client.post("/auth/device", json={"client_name": "another-cli"}).status_code == 201
            )
        assert client.post("/auth/device", json={"client_name": "excess-cli"}).status_code == 429

        for index in range(normal_polls, 120):
            response = client.post(
                "/auth/device/token",
                json={"device_code": devices[index % 2].device_code},
            )
            assert response.status_code == 200
        refused = client.post("/auth/device/token", json={"device_code": devices[0].device_code})
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) > 0


def test_device_codes_expire_and_are_pruned(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
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
