from __future__ import annotations

from contextlib import ExitStack
from typing import Protocol

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from identity.auth import AuthService
from pydantic import JsonValue, TypeAdapter
from shared.deployment_records import DeploymentSpec
from shared.identity import AuthScope, TokenKind

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _HttpResponse(Protocol):
    @property
    def content(self) -> bytes: ...


def test_app_and_deployment_http_actions_follow_authorization_and_lifecycle_state(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        app = isolated_services.apps.create("dashboard_app")
        deployment = isolated_services.deployments.deploy(
            DeploymentSpec(
                name="api",
                handler="pkg:api",
                metadata={"app": app.name, "app_id": app.id},
            )
        )
        workspace = ControlPlaneService(isolated_services.context).get_workspace(app.workspace_id)
        auth = AuthService(isolated_services.context)
        writer_token, _ = auth.create_token(
            "lifecycle-writer",
            scopes=[AuthScope.Read.value, AuthScope.Write.value],
            kind=TokenKind.Workspace,
            workspace_id=workspace.id,
        )
        reader_token, _ = auth.create_token(
            "lifecycle-reader",
            scopes=[AuthScope.Read.value],
            kind=TokenKind.Workspace,
            workspace_id=workspace.id,
        )
        client = client_stack.enter_context(TestClient(create_app(isolated_services)))
        writer_headers = {"Authorization": f"Bearer {writer_token}"}
        reader_headers = {"Authorization": f"Bearer {reader_token}"}

        writer_apps = client.get("/api/v1/apps/summaries", headers=writer_headers)
        assert writer_apps.status_code == 200, writer_apps.text
        writer_items = _json_path(_response_json(writer_apps), "items")
        assert isinstance(writer_items, list)
        writer_summary = next(
            item
            for item in writer_items
            if isinstance(item, dict) and _json_path(item, "app", "id") == app.id
        )
        assert _json_path(writer_summary, "app", "actions") == {
            "can_pause": True,
            "can_resume": False,
            "can_delete": True,
        }
        assert _json_path(writer_summary, "latest_deployment", "actions") == {
            "can_start": False,
            "can_stop": True,
            "can_scale": False,
            "can_delete": True,
        }

        reader_app = client.get(f"/api/v1/apps/{app.id}", headers=reader_headers)
        assert reader_app.status_code == 200, reader_app.text
        assert _json_path(_response_json(reader_app), "actions") == {
            "can_pause": False,
            "can_resume": False,
            "can_delete": False,
        }
        reader_deployment = client.get(
            f"/api/v1/deployments/{deployment.id}",
            headers=reader_headers,
        )
        assert reader_deployment.status_code == 200, reader_deployment.text
        assert _json_path(_response_json(reader_deployment), "actions") == {
            "can_start": False,
            "can_stop": False,
            "can_scale": False,
            "can_delete": False,
        }

        pause = client.post(f"/api/v1/apps/{app.id}/pause", headers=writer_headers)
        assert pause.status_code == 200, pause.text
        pause_payload = _response_json(pause)
        assert _json_path(pause_payload, "active") is False
        assert _json_path(pause_payload, "deleted_at") is None
        assert _json_path(pause_payload, "actions") == {
            "can_pause": False,
            "can_resume": True,
            "can_delete": True,
        }
        paused_apps = client.get("/api/v1/apps", headers=writer_headers)
        paused_items = _json_path(_response_json(paused_apps), "data")
        assert isinstance(paused_items, list)
        assert app.id in [_json_path(item, "id") for item in paused_items if isinstance(item, dict)]

        paused_deployment = client.get(
            f"/api/v1/deployments/{deployment.id}",
            headers=writer_headers,
        )
        assert paused_deployment.status_code == 200, paused_deployment.text
        assert _json_path(_response_json(paused_deployment), "actions") == {
            "can_start": False,
            "can_stop": False,
            "can_scale": False,
            "can_delete": True,
        }
        blocked_start = client.post(
            f"/api/v1/deployments/{deployment.id}/start",
            headers=writer_headers,
        )
        assert blocked_start.status_code == 409, blocked_start.text

        resume = client.post(f"/api/v1/apps/{app.id}/resume", headers=writer_headers)
        assert resume.status_code == 200, resume.text
        assert _json_path(_response_json(resume), "active") is True
        assert isolated_services.deployments.get(deployment.id).active

        deleted = client.delete(f"/api/v1/apps/{app.id}", headers=writer_headers)
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert client.get(f"/api/v1/apps/{app.id}", headers=writer_headers).status_code == 404
        deployments = client.get("/api/v1/deployments", headers=writer_headers)
        deployment_items = _json_path(_response_json(deployments), "data")
        assert isinstance(deployment_items, list)
        assert deployment.id not in [
            _json_path(item, "id") for item in deployment_items if isinstance(item, dict)
        ]


def _response_json(response: _HttpResponse) -> JsonValue:
    return _JSON_VALUE_ADAPTER.validate_json(response.content)


def _json_path(value: JsonValue, *path: str | int) -> JsonValue:
    current = value
    for segment in path:
        if isinstance(segment, str):
            assert isinstance(current, dict)
            current = current[segment]
        else:
            assert isinstance(current, list)
            current = current[segment]
    return current
