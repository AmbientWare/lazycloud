from __future__ import annotations

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from execution.endpoints.previews import PreviewSessionService
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.deployments import StubKind
from shared.http.gateway import DeployStubResponse
from shared.http.previews import (
    PreviewSessionResponse,
    PreviewSessionStatus,
    preview_invocation_path,
)
from starlette.websockets import WebSocketDisconnect
from tests.workspaces import owned_workspace


def test_preview_urls_and_controls_enforce_scope_and_lease(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    control = ControlPlaneService(services.context)
    owner = owned_workspace(control, "default")
    outsider = owned_workspace(control, "preview-outsider")
    token, _ = AuthService(services.context).create_token(
        "preview-owner", scopes=["read", "write"], workspace_id=owner.id
    )
    other_token, _ = AuthService(services.context).create_token(
        "preview-outsider", scopes=["read", "write"], workspace_id=outsider.id
    )
    headers = {"Authorization": f"Bearer {token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    source = control.create_stub(
        "private-preview",
        kind=StubKind.Endpoint,
        handler="module:handler",
        workspace=owner.id,
        config={"image": {"image_id": "image-web"}},
    )
    public_source = control.create_stub(
        "public-preview",
        kind=StubKind.Asgi,
        handler="module:app",
        workspace=owner.id,
        public=True,
        config={"image": {"image_id": "image-web"}},
    )
    with TestClient(create_app(services)) as client:
        assert client.get("/api/v1/previews/invalid", headers=headers).status_code == 404
        assert client.get("/api/v1/previews/public/invalid/invoke").status_code == 404
        assert (
            client.post(
                "/api/v1/previews", headers=headers, json={"stub_id": "invalid"}
            ).status_code
            == 422
        )
        created = client.post("/api/v1/previews", headers=headers, json={"stub_id": source.id})
        assert created.status_code == 201
        preview = PreviewSessionResponse.model_validate(created.json())
        deployed = client.post(
            "/gateway/stubs/deploy",
            headers=headers,
            json={"stub_id": preview.execution_stub_id, "name": "preview-published"},
        )
        assert deployed.status_code == 200
        deployment = DeployStubResponse.model_validate(deployed.json())
        assert deployment.stub_id not in {preview.execution_stub_id, source.id}
        assert (
            client.post(
                f"/api/v1/endpoints/id/{preview.execution_stub_id}", headers=headers, json={}
            ).status_code
            == 404
        )
        public_created = client.post(
            "/api/v1/previews", headers=headers, json={"stub_id": public_source.id}
        )
        assert public_created.status_code == 201
        public = PreviewSessionResponse.model_validate(public_created.json())
        path = f"/api/v1/previews/{preview.id}"
        assert client.get(path, headers=other_headers).status_code == 404
        assert client.delete(path, headers=other_headers).status_code == 404
        assert client.post(f"{path}/heartbeat", headers=other_headers).status_code == 404
        assert (
            client.post(preview_invocation_path(preview.id, public=False), json={}).status_code
            == 401
        )
        assert (
            client.post(preview_invocation_path(preview.id, public=True), json={}).status_code
            == 404
        )
        assert client.post(f"{path}/heartbeat", headers=headers).status_code == 200
        assert client.delete(path, headers=headers).status_code == 204
        assert client.delete(path, headers=headers).status_code == 204
        warmup = client.post(f"/api/v1/endpoints/id/{deployment.stub_id}/warmup", headers=headers)
        assert warmup.status_code == 200
        services.containers.stop(warmup.json()["container_id"])
        assert control.get_stub(deployment.stub_id).deployment_id == deployment.deployment_id
        assert (
            client.post(
                preview_invocation_path(preview.id, public=False), headers=headers, json={}
            ).status_code
            == 404
        )
        services.redis_client.delete(PreviewSessionService(services).lease_key(public.id))
        assert (
            client.get(preview_invocation_path(public.id, public=True) + "/health").status_code
            == 404
        )
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect(preview_invocation_path(public.id, public=True)),
        ):
            pytest.fail("expired preview websocket was admitted")
        assert closed.value.code == 1008
        assert PreviewSessionService(services).get(public.id).status is PreviewSessionStatus.Expired
        assert (
            client.post(f"/api/v1/previews/{public.id}/heartbeat", headers=headers).status_code
            == 404
        )
        assert client.delete(f"/api/v1/previews/{public.id}", headers=headers).status_code == 204
