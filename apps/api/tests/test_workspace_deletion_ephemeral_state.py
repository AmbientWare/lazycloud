from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from api.server.workspace_deletion import _delete_workspace_workload_state
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from tests.workspaces import administrator_credential, owned_workspace

_WORKLOAD_KEY_ROOTS: tuple[tuple[str, ...], ...] = (
    ("endpoint",),
    ("function",),
    ("pod",),
    ("task",),
    ("scheduler", "serve", "lock"),
)


def test_workspace_deletion_removes_only_its_ephemeral_workload_state(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    control.get_workspace("default")
    deleted_workspace = owned_workspace(control, "ephemeral-cleanup")
    peer_workspace = owned_workspace(control, "ephemeral-cleanup-peer")
    redis = isolated_services.redis()

    deleted_keys = {
        redis.key(*root, deleted_workspace.id, "stub", "state") for root in _WORKLOAD_KEY_ROOTS
    }
    peer_keys = {
        redis.key(*root, peer_workspace.id, "stub", "state") for root in _WORKLOAD_KEY_ROOTS
    }
    unrelated_key = redis.key("coordination", deleted_workspace.id, "state")
    for key in deleted_keys | peer_keys | {unrelated_key}:
        assert redis.set(key, "present")

    admin_token, _record = administrator_credential(
        isolated_services.context, "ephemeral-cleanup-admin"
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    response = client.delete(
        f"/api/v1/workspaces/{deleted_workspace.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 204, response.text
    assert all(not redis.exists(key) for key in deleted_keys)
    assert all(redis.exists(key) for key in peer_keys)
    assert redis.exists(unrelated_key)
    assert _delete_workspace_workload_state(redis, deleted_workspace.id) == 0
    assert all(redis.exists(key) for key in peer_keys)
    assert redis.exists(unrelated_key)
