from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import ExitStack
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from api.server.workspace_deletion import _delete_workspace_workload_state
from control.service import ControlPlaneService
from coordination.event_bus import EventBusEventType
from coordination.redis_client import RedisClient, redis_text
from database.repositories.orchestration import ContainerRepository
from fastapi.testclient import TestClient
from scheduler.state import RedisSchedulerContainerRepository
from shared.containers import ContainerRecord, ContainerStatus
from tests.service_fixtures import administrator_credential, owned_workspace

_WORKLOAD_KEY_ROOTS: tuple[tuple[str, ...], ...] = (
    ("endpoint",),
    ("function",),
    ("pod",),
    ("task",),
    ("taskqueue",),
    ("scheduler", "serve", "lock"),
)


def _stop_container_event_count(redis: RedisClient, pattern: str) -> int:
    """Stop-container events on the bus.

    Workspace deletion also wakes global source-cache reconciliation, which is a
    different owner, so the bus is filtered by event type rather than required to
    stay empty.
    """
    count = 0
    for key in redis.scan(pattern):
        raw = redis.get(key)
        if raw is None:
            continue
        payload = json.loads(redis_text(raw))
        if payload.get("type") == EventBusEventType.StopContainer:
            count += 1
    return count


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

    admin_token, _record = administrator_credential(isolated_services, "ephemeral-cleanup-admin")
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


def test_workspace_deletion_ignores_terminal_container_history_with_stale_workers(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    control.get_workspace("default")
    deleted_workspace = owned_workspace(control, "terminal-container-cleanup")
    peer_workspace = owned_workspace(control, "terminal-container-peer")
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
    delivery_patterns = (
        redis.key("worker-events", "pending", "*"),
        redis.key("worker-events", "ack", "*"),
    )
    delivery_keys_before = {pattern: set(redis.scan(pattern)) for pattern in delivery_patterns}
    event_bus_pattern = redis.key("event", "*")
    admin_token, _record = administrator_credential(
        isolated_services, "terminal-container-cleanup-admin"
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.delete(
        f"/api/v1/workspaces/{deleted_workspace.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 204, response.text
    assert {
        pattern: set(redis.scan(pattern)) for pattern in delivery_patterns
    } == delivery_keys_before
    assert _stop_container_event_count(redis, event_bus_pattern) == 0
    assert cleaned_scheduler_state == [(deleted_workspace.id, deleted_container_ids)]
    with isolated_services.context.database.session() as session:
        assert (
            ContainerRepository(session).get(
                peer_container_id,
                workspace_id=peer_workspace.id,
            )
            is not None
        )
