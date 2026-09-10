from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass

import api.server.routers.shells as shell_router
import pytest
from api.fastapi_app import create_app
from api.server.service_dependencies import shell_service
from api.server.services import ApiServices
from execution.shells.service import (
    ShellTicketCompensationResult,
    ShellTicketCompensationStatus,
)
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.containers import ContainerStatus
from shared.http.errors import ErrorResponse
from shared.http.shells import (
    CreateShellInExistingContainerResponse,
    CreateStandaloneShellResponse,
    ExistingContainerShellSession,
    StandaloneShellSession,
)


@dataclass(slots=True)
class _ShellService:
    compensation_succeeds: bool = True

    def create_standalone_shell(
        self,
        *,
        workspace_id: str,
        stub_id: str,
    ) -> StandaloneShellSession:
        return StandaloneShellSession(
            container_id=f"container-{stub_id}",
            username="shell",
            password="password",
        )

    def create_shell_in_existing_container(
        self,
        *,
        workspace_id: str,
        container_id: str,
    ) -> ExistingContainerShellSession:
        return ExistingContainerShellSession(
            username="shell",
            password="password",
            stub_id=f"stub-{container_id}",
        )

    def compensate_standalone_ticket_failure(
        self,
        *,
        workspace_id: str,
        container_id: str,
    ) -> ShellTicketCompensationResult:
        return ShellTicketCompensationResult(
            container_id=container_id,
            status=(
                ShellTicketCompensationStatus.Cleaned
                if self.compensation_succeeds
                else ShellTicketCompensationStatus.Failed
            ),
            terminal_status=(ContainerStatus.Stopped if self.compensation_succeeds else None),
            failure_recorded=not self.compensation_succeeds,
        )

    def compensate_existing_container_ticket_failure(
        self,
        *,
        workspace_id: str,
        container_id: str,
    ) -> ShellTicketCompensationResult:
        return ShellTicketCompensationResult(
            container_id=container_id,
            status=(
                ShellTicketCompensationStatus.Cleaned
                if self.compensation_succeeds
                else ShellTicketCompensationStatus.Failed
            ),
            failure_recorded=not self.compensation_succeeds,
        )


def test_shell_creation_responses_replace_bearers_with_distinct_tickets(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        app = create_app(isolated_services)
        service = _ShellService()
        app.dependency_overrides[shell_service] = lambda: service
        client = client_stack.enter_context(TestClient(app))
        bearer = _offline_admin_token(isolated_services, "bootstrap:shell-ticket-responses")
        headers = {"Authorization": f"Bearer {bearer}"}

        standalone = client.post(
            "/api/v1/shells/standalone",
            headers=headers,
            json={"stub_id": "stub-1"},
        )
        existing = client.post(
            "/api/v1/shells/existing-container",
            headers=headers,
            json={"container_id": "container-1"},
        )

        assert standalone.status_code == 200
        assert existing.status_code == 200
        standalone_ticket = CreateStandaloneShellResponse.model_validate_json(
            standalone.content
        ).websocket_ticket
        existing_ticket = CreateShellInExistingContainerResponse.model_validate_json(
            existing.content
        ).websocket_ticket
        assert standalone_ticket
        assert existing_ticket
        assert standalone_ticket != existing_ticket
        assert bearer not in standalone.text
        assert bearer not in existing.text
        ticket_keys = isolated_services.redis_client.scan(
            isolated_services.redis_client.key("identity", "websocket-ticket", "*")
        )
        assert len(ticket_keys) == 2


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/v1/shells/standalone",
            {"stub_id": "stub-1"},
        ),
        (
            "/api/v1/shells/existing-container",
            {"container_id": "container-1"},
        ),
    ],
)
def test_ticket_store_failure_returns_unavailable_without_leaking_ticket_or_store_details(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, str],
) -> None:
    with ExitStack() as client_stack:
        app = create_app(isolated_services)
        service = _ShellService()
        app.dependency_overrides[shell_service] = lambda: service
        client = client_stack.enter_context(TestClient(app))
        bearer = _offline_admin_token(isolated_services, "bootstrap:shell-ticket-store-failure")

        def fail_ticket_mint(*_args: object, **_kwargs: object) -> str:
            raise OSError("private redis endpoint")

        monkeypatch.setattr(shell_router, "_mint_shell_ticket", fail_ticket_mint)
        response = client.post(
            path,
            headers={"Authorization": f"Bearer {bearer}"},
            json=body,
        )

        assert response.status_code == 503
        assert ErrorResponse.model_validate_json(response.content).detail == (
            "Shell session authorization is temporarily unavailable"
        )
        assert "redis" not in response.text.lower()
        ticket_keys = isolated_services.redis_client.scan(
            isolated_services.redis_client.key("identity", "websocket-ticket", "*")
        )
        assert not ticket_keys


def test_ticket_store_and_compensation_failure_surface_both_facts_safely(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ExitStack() as client_stack:
        app = create_app(isolated_services)
        service = _ShellService(
            compensation_succeeds=False,
        )
        app.dependency_overrides[shell_service] = lambda: service
        client = client_stack.enter_context(TestClient(app))
        bearer = _offline_admin_token(isolated_services, "bootstrap:shell-ticket-cleanup-failure")

        def fail_ticket_mint(*_args: object, **_kwargs: object) -> str:
            raise OSError("private redis endpoint")

        monkeypatch.setattr(shell_router, "_mint_shell_ticket", fail_ticket_mint)
        response = client.post(
            "/api/v1/shells/standalone",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"stub_id": "stub-1"},
        )

        assert response.status_code == 503
        assert ErrorResponse.model_validate_json(response.content).detail == (
            "Shell session authorization is temporarily unavailable; "
            "shell resource cleanup also failed"
        )
        assert "redis" not in response.text.lower()


def _offline_admin_token(services: ApiServices, request_id: str) -> str:
    return AuthService(services.context).bootstrap_administrator(request_id=request_id).token
