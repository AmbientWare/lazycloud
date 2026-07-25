from __future__ import annotations

from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from identity.auth import AuthService
from images.control import ImageControlService
from shared.http.images import VerifyImageBuildResponse
from shared.http.operations import ImageBuildListResponse, ImageBuildResponse
from shared.identity import TokenKind
from storage.service import ObjectStorage
from tests.fakes import FakeObjectClient


def test_image_build_http_records_events_and_context_are_workspace_owned(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    first_workspace = control.upsert_workspace("image-build-first")
    second_workspace = control.upsert_workspace("image-build-second")
    first_token = _workspace_token(isolated_services, first_workspace.id, "image-build-first")
    second_token = _workspace_token(isolated_services, second_workspace.id, "image-build-second")
    object_storage = ObjectStorage(
        isolated_services.context,
        object_client=FakeObjectClient(),
        default_bucket="objects",
    )
    client_stack = ExitStack()
    request.addfinalizer(client_stack.close)
    client = client_stack.enter_context(
        TestClient(
            create_app(
                isolated_services,
                image_service=ImageControlService(
                    isolated_services,
                    build_context_reader=object_storage,
                ),
            )
        )
    )

    first = client.post(
        "/api/v1/image-builds",
        headers=_headers(first_token),
        json={"image": {"base": "scratch", "ignore_python": True}},
    )
    second = client.post(
        "/api/v1/image-builds",
        headers=_headers(second_token),
        json={"image": {"base": "scratch", "ignore_python": True}},
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_build_id = ImageBuildResponse.model_validate_json(first.content).id
    second_build_id = ImageBuildResponse.model_validate_json(second.content).id
    assert first_build_id != second_build_id

    first_list = client.get("/api/v1/image-builds", headers=_headers(first_token))
    second_list = client.get("/api/v1/image-builds", headers=_headers(second_token))
    first_foreign = client.get(
        f"/api/v1/image-builds/{second_build_id}",
        headers=_headers(first_token),
    )
    second_foreign = client.get(
        f"/api/v1/image-builds/{first_build_id}",
        headers=_headers(second_token),
    )

    first_builds = ImageBuildListResponse.model_validate_json(first_list.content).builds
    second_builds = ImageBuildListResponse.model_validate_json(second_list.content).builds
    assert [item.id for item in first_builds] == [first_build_id]
    assert [item.id for item in second_builds] == [second_build_id]
    assert first_foreign.status_code == 404
    assert second_foreign.status_code == 404

    first_events = isolated_services.events.list(workspace_id=first_workspace.id)
    second_events = isolated_services.events.list(workspace_id=second_workspace.id)
    assert {
        event.resource_id for event in first_events if event.resource_type == "image_build"
    } == {first_build_id}
    assert {
        event.resource_id for event in second_events if event.resource_type == "image_build"
    } == {second_build_id}

    context = object_storage.put_bytes_for_workspace(
        workspace_id=first_workspace.id,
        bucket="objects",
        key="contexts/private.zip",
        data=b"private build context",
    )
    foreign_context = client.post(
        "/api/v1/images/verify-build",
        headers=_headers(second_token),
        json={
            "build_ctx_object": context.id,
            "build_ctx_digest": context.sha256,
        },
    )

    assert foreign_context.status_code == 200
    verification = VerifyImageBuildResponse.model_validate_json(foreign_context.content)
    assert verification.valid is False
    assert "not found" in verification.reason


def _workspace_token(isolated_services: ApiServices, workspace_id: str, name: str) -> str:
    token, _record = AuthService(isolated_services.context).create_token(
        name,
        kind=TokenKind.Workspace,
        workspace_id=workspace_id,
    )
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
