from __future__ import annotations

import logging
from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.http.errors import ErrorResponse
from shared.identity import TokenKind


def _client(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    *,
    raise_server_exceptions: bool = True,
) -> tuple[FastAPI, TestClient, dict[str, str]]:
    raw_token, _ = AuthService(isolated_services.context).create_token(
        "error-handling-admin",
        kind=TokenKind.Admin,
    )
    app = create_app(isolated_services)
    client = client_stack.enter_context(
        TestClient(app, raise_server_exceptions=raise_server_exceptions)
    )
    return app, client, {"Authorization": f"Bearer {raw_token}"}


def test_missing_resources_return_typed_not_found(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    _, client, headers = _client(isolated_services, client_stack)
    missing = "3e1f5a52-9d5c-4b57-9c25-1e35a0f2f6a1"

    for path in (
        f"/api/v1/tasks/{missing}",
        f"/api/v1/containers/{missing}",
        "/api/v1/secrets/does-not-exist",
        "/api/v1/apps/does-not-exist",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 404, path
        body = ErrorResponse.model_validate_json(response.content)
        assert "Traceback" not in body.detail, path


def test_invalid_deployment_version_returns_typed_invalid_input(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    _, client, headers = _client(isolated_services, client_stack)
    response = client.get(
        "/api/v1/deployments/by-name/endpoint/some-deployment/not-a-number/url",
        headers=headers,
    )
    assert response.status_code == 400
    assert ErrorResponse.model_validate_json(response.content).detail == (
        "invalid deployment version: not-a-number"
    )


def test_invalid_stub_type_returns_typed_invalid_input(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    _, client, headers = _client(isolated_services, client_stack)
    response = client.get(
        "/api/v1/deployments/by-name/bogus-kind/some-deployment/latest/url",
        headers=headers,
    )
    assert response.status_code == 400
    assert ErrorResponse.model_validate_json(response.content).detail == (
        "invalid stub type: bogus-kind"
    )


def test_unexpected_exception_returns_opaque_500(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, client, headers = _client(
        isolated_services,
        client_stack,
        raise_server_exceptions=False,
    )
    router = APIRouter()

    @router.get("/api/v1/error-handling-probe")
    def _explode() -> None:
        msg = "sensitive internal detail"
        raise RuntimeError(msg)

    app.include_router(router)

    with caplog.at_level(logging.ERROR, logger="api.fastapi_app"):
        response = client.get(
            "/api/v1/error-handling-probe",
            headers={**headers, "x-request-id": "probe-request-id"},
        )

    assert response.status_code == 500
    body = ErrorResponse.model_validate_json(response.content)
    assert "sensitive internal detail" not in body.detail
    assert "RuntimeError" not in body.detail
    assert "probe-request-id" in body.detail
    assert response.headers["x-request-id"] == "probe-request-id"

    matching = [
        record
        for record in caplog.records
        if record.name == "api.fastapi_app" and "probe-request-id" in record.getMessage()
    ]
    assert matching, "unexpected error must be logged server-side with the request id"
    assert any(
        record.exc_info is not None and "sensitive internal detail" in str(record.exc_info[1])
        for record in matching
    )


def test_unexpected_exception_mints_request_id_when_absent(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    app, client, headers = _client(
        isolated_services,
        client_stack,
        raise_server_exceptions=False,
    )
    router = APIRouter()

    @router.get("/api/v1/error-handling-probe-no-id")
    def _explode() -> None:
        raise RuntimeError("boom")

    app.include_router(router)

    response = client.get("/api/v1/error-handling-probe-no-id", headers=headers)
    assert response.status_code == 500
    minted = response.headers["x-request-id"]
    assert minted
    assert minted in ErrorResponse.model_validate_json(response.content).detail
