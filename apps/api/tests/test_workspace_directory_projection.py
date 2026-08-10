from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.identity import WorkspaceRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.http.workspaces import WorkspaceListResponse
from shared.identity import WorkspaceStatus
from tests.service_fixtures import administrator_credential


def test_admin_current_workspace_honors_explicit_workspace_override(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    control.get_workspace("default")
    target = control.upsert_workspace("provider-acceptance")
    admin_token, _ = administrator_credential(isolated_services, "workspace-override-admin")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get(
        "/api/v1/workspaces/current",
        params={"workspace": target.name},
        headers=_auth(admin_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["id"] == target.id
    assert response.json()["name"] == target.name


def test_admin_can_include_deleting_workspaces_but_not_deleted_tombstones(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    control.get_workspace("default")
    active = control.upsert_workspace("projection-active")
    deleting = control.upsert_workspace("projection-deleting")
    deleted = control.upsert_workspace("projection-deleted")
    _set_lifecycle_states(
        isolated_services,
        deleting_workspace_id=deleting.id,
        deleted_workspace_id=deleted.id,
    )
    admin_token, _ = administrator_credential(isolated_services, "directory-admin")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get(
        "/api/v1/workspaces",
        params={"include_deleting": "true"},
        headers=_auth(admin_token),
    )

    assert response.status_code == 200, response.text
    assert _projection(response.content) == {
        "default": WorkspaceStatus.Active,
        active.name: WorkspaceStatus.Active,
        deleting.name: WorkspaceStatus.Deleting,
    }


def test_workspace_token_cannot_expand_its_workspace_directory(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    active = control.upsert_workspace("projection-active")
    deleting = control.upsert_workspace("projection-deleting")
    deleted = control.upsert_workspace("projection-deleted")
    token, _ = AuthService(isolated_services.context).create_token(
        "directory-workspace",
        workspace_id=active.id,
    )
    _set_lifecycle_states(
        isolated_services,
        deleting_workspace_id=deleting.id,
        deleted_workspace_id=deleted.id,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get(
        "/api/v1/workspaces",
        params={"include_deleting": "true", "include_deleted": "true"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert _projection(response.content) == {
        active.name: WorkspaceStatus.Active,
    }


def _set_lifecycle_states(
    services: ApiServices,
    *,
    deleting_workspace_id: str,
    deleted_workspace_id: str,
) -> None:
    with services.context.database.session() as session:
        repository = WorkspaceRepository(session)
        deleting = repository.get(deleting_workspace_id)
        deleted = repository.get(deleted_workspace_id)
        assert deleting is not None
        assert deleted is not None
        repository.mark_deleting(deleting)
        repository.tombstone(repository.mark_deleting(deleted))


def _projection(content: bytes) -> dict[str, WorkspaceStatus]:
    response = WorkspaceListResponse.model_validate_json(content)
    return {workspace.name: workspace.status for workspace in response.workspaces}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
