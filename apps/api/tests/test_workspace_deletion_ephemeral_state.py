from __future__ import annotations

from collections.abc import Iterable
from contextlib import ExitStack
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from api.server.workspace_deletion import _delete_workspace_workload_state
from control.service import ControlPlaneService
from database.repositories.orchestration import ContainerRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from scheduler.state import RedisSchedulerContainerRepository
from shared.containers import ContainerRecord, ContainerStatus
from shared.identity import TokenKind

_WORKLOAD_KEY_ROOTS: tuple[tuple[str, ...], ...] = (
    ("endpoint",),
    ("function",),
    ("pod",),
    ("task",),
    ("taskqueue",),
    ("scheduler", "serve", "lock"),
)


def test_workspace_deletion_removes_only_its_ephemeral_workload_state(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    default_workspace = control.get_workspace("default")
    deleted_workspace = control.upsert_workspace("ephemeral-cleanup")
    peer_workspace = control.upsert_workspace("ephemeral-cleanup-peer")
    redis = isolated_services.redis()

    deleted_keys = {
        redis.key(*root, deleted_workspace.name, "stub", "state") for root in _WORKLOAD_KEY_ROOTS
    }
    peer_keys = {
        redis.key(*root, peer_workspace.name, "stub", "state") for root in _WORKLOAD_KEY_ROOTS
    }
    unrelated_key = redis.key("coordination", deleted_workspace.name, "state")
    for key in deleted_keys | peer_keys | {unrelated_key}:
        assert redis.set(key, "present")

    admin_token, _record = AuthService(isolated_services.context).create_token(
        "ephemeral-cleanup-admin",
        kind=TokenKind.Admin,
        workspace_id=default_workspace.id,
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
    assert _delete_workspace_workload_state(redis, deleted_workspace.name) == 0
    assert all(redis.exists(key) for key in peer_keys)
    assert redis.exists(unrelated_key)


def test_workspace_deletion_ignores_terminal_container_history_with_stale_workers(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    default_workspace = control.get_workspace("default")
    deleted_workspace = control.upsert_workspace("terminal-container-cleanup")
    peer_workspace = control.upsert_workspace("terminal-container-peer")
    peer_container_id = str(uuid4())
    deleted_container_ids: set[str] = set()

    with isolated_services.context.database.session() as session:
        repository = ContainerRepository(session)
        for status in (
            ContainerStatus.Exited,
            ContainerStatus.Failed,
            ContainerStatus.Stopped,
        ):
            container_id = str(uuid4())
            deleted_container_ids.add(container_id)
            repository.upsert(
                ContainerRecord(
                    id=container_id,
                    name=f"terminal-{status.value}",
                    image="",
                    command=[],
                    workspace_id=deleted_workspace.id,
                    runtime_worker_id=f"stale-{status.value}-worker",
                    status=status,
                )
            )
        repository.upsert(
            ContainerRecord(
                id=peer_container_id,
                name="peer-terminal",
                image="",
                command=[],
                workspace_id=peer_workspace.id,
                runtime_worker_id="peer-stale-worker",
                status=ContainerStatus.Exited,
            )
        )

    cleaned_scheduler_state: list[tuple[str, set[str]]] = []

    def delete_workspace_container_state(
        _repository: RedisSchedulerContainerRepository,
        workspace_id: str,
        *,
        container_ids: Iterable[str] = (),
    ) -> int:
        cleaned_scheduler_state.append((workspace_id, set(container_ids)))
        return 0

    monkeypatch.setattr(
        RedisSchedulerContainerRepository,
        "delete_workspace_container_state",
        delete_workspace_container_state,
    )

    redis = isolated_services.redis()
    event_patterns = (
        redis.key("event", "*"),
        redis.key("worker-events", "pending", "*"),
        redis.key("worker-events", "ack", "*"),
    )
    event_keys_before = {pattern: set(redis.scan(pattern)) for pattern in event_patterns}
    admin_token, _record = AuthService(isolated_services.context).create_token(
        "terminal-container-cleanup-admin",
        kind=TokenKind.Admin,
        workspace_id=default_workspace.id,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.delete(
        f"/api/v1/workspaces/{deleted_workspace.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 204, response.text
    assert {pattern: set(redis.scan(pattern)) for pattern in event_patterns} == event_keys_before
    assert cleaned_scheduler_state == [(deleted_workspace.id, deleted_container_ids)]
    with isolated_services.context.database.session() as session:
        assert (
            ContainerRepository(session).get(
                peer_container_id,
                workspace_id=peer_workspace.id,
            )
            is not None
        )
