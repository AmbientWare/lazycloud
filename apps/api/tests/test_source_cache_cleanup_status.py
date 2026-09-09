from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from uuid import uuid4

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.source_cache import SourceCacheCleanupRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.http.source_cache_cleanup import SourceCacheCleanupStatusResponse
from shared.identity import TokenKind
from tests.domain_fixtures import owned_workspace
from tests.service_fixtures import administrator_credential


def test_source_cache_cleanup_status_is_admin_only_and_bounded(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    default_workspace = control.get_workspace("default")
    target = owned_workspace(control, "cleanup-status-target")
    started_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    with isolated_services.context.database.session() as session:
        repository = SourceCacheCleanupRepository(session)
        repository.register_generation(
            str(uuid4()),
            worker_id="worker-a",
            storage_id="physical-cache-a",
            workspace_id=None,
            now=started_at,
        )
        repository.add_targets(
            workspace_id=target.id,
            source_object_ids=[str(uuid4())],
            now=started_at,
        )

    auth = AuthService(isolated_services.context)
    admin_token, _admin = administrator_credential(isolated_services, "cleanup-status-admin")
    workspace_token, _workspace = auth.create_token(
        "cleanup-status-workspace",
        kind=TokenKind.Workspace,
        workspace_id=default_workspace.id,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    path = f"/api/v1/workspaces/{target.name}/source-cache-cleanup"

    denied = client.get(path, headers=_auth(workspace_token))
    response = client.get(path, headers=_auth(admin_token))

    assert denied.status_code == 403
    assert response.status_code == 200, response.text
    status = SourceCacheCleanupStatusResponse.model_validate_json(response.content)
    assert status.workspace_id == target.id
    assert status.pending_count == 1
    assert status.claimed_count == 0
    assert status.completed_count == 0
    assert status.generations_pending == 1
    assert status.oldest_pending_age_seconds is not None
    assert status.oldest_pending_age_seconds >= 0
    assert not status.complete
    assert set(response.json()) == {
        "workspace_id",
        "pending_count",
        "claimed_count",
        "completed_count",
        "generations_pending",
        "failing_count",
        "last_error_code",
        "oldest_pending_age_seconds",
        "complete",
    }


def test_source_cache_cleanup_status_returns_not_found_for_unknown_workspace(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    ControlPlaneService(isolated_services.context).get_workspace("default")
    admin_token, _record = administrator_credential(isolated_services, "cleanup-status-admin")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get(
        "/api/v1/workspaces/missing-workspace/source-cache-cleanup",
        headers=_auth(admin_token),
    )

    assert response.status_code == 404


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
