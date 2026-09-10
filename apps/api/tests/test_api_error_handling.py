from __future__ import annotations

import logging
from contextlib import ExitStack
from dataclasses import dataclass, field, replace

import pytest
from api.fastapi_app import create_app
from api.server.provider_compute import (
    BoundedProviderNodeIdentityHttpClient,
    RedisProviderNodeIdentityReplayGuard,
)
from api.server.services import ApiServices
from control.service import WorkspaceStorageError
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from provider_clients import AwsProviderNodeIdentityAdapter
from shared.external_identity import ExternalIdentityProfile
from shared.http.errors import ErrorResponse
from shared.identity import IdentityProvider, WorkspaceRecord
from tests.workspaces import administrator_credential


def _client(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    *,
    raise_server_exceptions: bool = True,
) -> tuple[FastAPI, TestClient, dict[str, str]]:
    raw_token, _ = administrator_credential(isolated_services.context, "error-handling-admin")
    app = create_app(isolated_services)
    client = client_stack.enter_context(
        TestClient(app, raise_server_exceptions=raise_server_exceptions)
    )
    return app, client, {"Authorization": f"Bearer {raw_token}"}


def test_missing_resources_return_typed_not_found(
    api_client: TestClient,
) -> None:
    missing = "3e1f5a52-9d5c-4b57-9c25-1e35a0f2f6a1"

    for path in (
        f"/api/v1/tasks/{missing}",
        f"/api/v1/containers/{missing}",
        "/api/v1/secrets/does-not-exist",
        "/api/v1/apps/does-not-exist",
    ):
        response = api_client.get(path)
        assert response.status_code == 404, path
        body = ErrorResponse.model_validate_json(response.content)
        assert "Traceback" not in body.detail, path


def test_invalid_deployment_version_returns_typed_invalid_input(
    api_client: TestClient,
) -> None:
    response = api_client.get(
        "/api/v1/deployments/by-name/endpoint/some-deployment/not-a-number/url",
    )
    assert response.status_code == 400
    assert ErrorResponse.model_validate_json(response.content).detail == (
        "invalid deployment version: not-a-number"
    )


def test_invalid_stub_type_returns_typed_invalid_input(
    api_client: TestClient,
) -> None:
    response = api_client.get(
        "/api/v1/deployments/by-name/bogus-kind/some-deployment/latest/url",
    )
    assert response.status_code == 400
    assert ErrorResponse.model_validate_json(response.content).detail == (
        "invalid stub type: bogus-kind"
    )


def test_provider_validation_does_not_echo_identity_credentials(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enrollment = ProviderNodeEnrollmentService(
        gateway=isolated_services.gateway_service,
        compute=isolated_services.compute,
        identity_verifier=AwsProviderNodeIdentityAdapter(
            http_client=BoundedProviderNodeIdentityHttpClient(),
            replay_guard=RedisProviderNodeIdentityReplayGuard(isolated_services.redis()),
        ),
    )
    _, client, _ = _client(
        replace(isolated_services, provider_node_enrollment_service=enrollment), client_stack
    )
    proof = "https://identity.invalid/?credential=validation-proof-sentinel"
    response = client.post(
        "/gateway/provider-nodes/bootstrap-phase",
        json={
            "enrollment_request_id": "11111111-1111-4111-8111-111111111111",
            "provider": "aws",
            "region": "invalid-region",
            "provider_instance_id": "i-0123456789abcdef0",
            "identity_proof_url": proof,
            "phase": "booting",
        },
    )
    assert response.status_code == 422
    assert ErrorResponse.model_validate_json(response.content).code == "invalid_input"
    assert "validation-proof-sentinel" not in response.text
    assert "validation-proof-sentinel" not in caplog.text


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


@dataclass(slots=True)
class _StubIdentityProvider:
    """The provider half of the sign-in round trip, answering with one person."""

    states: list[str] = field(default_factory=list)

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        del code_challenge
        self.states.append(state)
        return f"https://identity.invalid/authorize?state={state}"

    def identify(self, *, code: str, code_verifier: str) -> ExternalIdentityProfile:
        del code, code_verifier
        return ExternalIdentityProfile(
            provider=IdentityProvider.Github,
            subject="99001",
            login="storage-failed",
            display_name="Storage Failed",
            email="storage-failed@example.invalid",
        )


def test_a_sign_in_that_cannot_be_provisioned_lands_the_browser_on_the_sign_in_page(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """The one route whose caller is a browser mid-navigation, not a client.

    Provisioning reaches object storage and the payment provider, and neither
    fails with a domain error — a bucket that cannot be created raises
    `WorkspaceStorageError`, which is a plain `RuntimeError`. Answered as an
    `ErrorResponse` it would be rendered to the person as a bare JSON document
    with no way forward, which is the whole reason this route redirects.
    """

    identity = _StubIdentityProvider()
    isolated_services.sign_in.provider_factory = lambda: identity

    def storage_unavailable(user_id: str, login: str) -> WorkspaceRecord:
        del user_id, login
        raise WorkspaceStorageError("unable to create workspace storage bucket 'workspace-1'")

    isolated_services.sign_in.provision_default_workspace = storage_unavailable
    _, client, _ = _client(isolated_services, client_stack, raise_server_exceptions=False)

    isolated_services.sign_in.start()
    response = client.get(
        "/auth/github/callback",
        params={"code": "auth-code", "state": identity.states[-1]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/signin?error=provider_unavailable"
