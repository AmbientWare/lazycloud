from __future__ import annotations

from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from shared.identity import WorkspaceRecord
from tests.workspaces import administrator_credential, owned_workspace

_WORKLOAD_KEY_ROOTS: tuple[tuple[str, ...], ...] = (
    ("endpoint",),
    ("function",),
    ("pod",),
    ("task",),
    ("scheduler", "serve", "lock"),
)


def test_workspace_deletion_removes_only_its_ephemeral_workload_state(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
) -> None:
    services, client = api_runtime
    control = ControlPlaneService(services.context)
    deleted_workspace = api_workspace
    peer_workspace = owned_workspace(control, f"ephemeral-cleanup-peer-{api_workspace.id}")
    redis = services.redis()

    deleted_keys = {
        redis.key(*root, deleted_workspace.id, "stub", "state") for root in _WORKLOAD_KEY_ROOTS
    }
    peer_keys = {
        redis.key(*root, peer_workspace.id, "stub", "state") for root in _WORKLOAD_KEY_ROOTS
    }
    unrelated_key = redis.key("coordination", deleted_workspace.id, "state")
    for key in deleted_keys | peer_keys | {unrelated_key}:
        assert redis.set(key, "present")

    admin_token, _record = administrator_credential(services.context, "ephemeral-cleanup-admin")
    response = client.delete(
        f"/api/v1/workspaces/{deleted_workspace.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 204, response.text
    assert all(not redis.exists(key) for key in deleted_keys)
    assert all(redis.exists(key) for key in peer_keys)
    assert redis.exists(unrelated_key)
