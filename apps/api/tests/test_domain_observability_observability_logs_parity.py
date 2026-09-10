from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from observability.stream_state import (
    RedisEventStreamRepository,
)
from pydantic import JsonValue
from shared.deployment_records import DeploymentSpec
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import LogStreamQuery
from shared.timestamps import utc_now
from tests.real_redis import RealRedisActors
from tests.workspaces import administrator_credential


def test_api_log_history_and_stream_support_filters_wait_and_resume(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    with ExitStack() as client_stack:
        with isolated_services.context.database.session() as session:
            workspace_id = isolated_services.context.default_workspace_id(session)
        task = isolated_services.tasks.create("durable-log", workspace_id=workspace_id)
        redis = real_redis_actors.client()
        repo = RedisEventStreamRepository(redis)
        _append_container_log(repo, message="needle first", workspace_id=workspace_id)
        first_cursor = repo.read_logs(LogStreamQuery(workspace_id=workspace_id))[-1].entry_id
        _append_container_log(repo, message="needle second", workspace_id=workspace_id)
        second_cursor = repo.read_logs(LogStreamQuery(workspace_id=workspace_id))[-1].entry_id
        client = client_stack.enter_context(TestClient(create_app(isolated_services)))
        admin_token, _record = administrator_credential(isolated_services.context, "root")
        for message in ("needle durable", ["needle durable second\n", "needle durable third\n"]):
            appended = client.post(
                "/gateway/tasks/log",
                params={"workspace": workspace_id},
                json={"task_id": task.id, "stream": "stdout", "message": message},
                headers=_auth(admin_token),
            )
            assert appended.status_code == 200, appended.text

        history = client.get(
            f"/api/v1/logs?workspace={workspace_id}&task_id={task.id}&query=durable",
            headers=_auth(admin_token),
        )
        stream = client.get(
            "/api/v1/logs/stream"
            f"?workspace_id={workspace_id}"
            "&follow=true&max_events=1&wait=2&container_id=container-1",
            headers=_auth(admin_token) | {"Last-Event-ID": first_cursor},
        )

        assert history.status_code == 200
        payload = history.json()
        assert [item["message"] for item in payload["data"]] == [
            "needle durable",
            "needle durable second",
            "needle durable third",
        ]
        assert payload["data"][0]["task_id"] == task.id
        assert stream.status_code == 200
        assert f"id: {second_cursor}" in stream.text
        assert "needle second" in stream.text


def test_api_deployment_logs_resolve_deployment_to_owned_stream(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    with ExitStack() as client_stack:
        redis = real_redis_actors.client()
        deployment = isolated_services.deployments.deploy(
            DeploymentSpec(name="deployment-logs", handler="pkg.module:handler")
        )
        assert deployment.app_id
        assert deployment.stub_id
        with isolated_services.context.database.session() as session:
            workspace_id = isolated_services.context.default_workspace_id(session)
        _append_container_log(
            RedisEventStreamRepository(redis),
            message="deployment line",
            workspace_id=workspace_id,
            stub_id=deployment.stub_id,
            app_id=deployment.app_id,
        )
        task = isolated_services.tasks.create(
            "deployment-log",
            workspace_id=workspace_id,
            app_id=deployment.app_id,
            stub_id=deployment.stub_id,
            deployment_id=deployment.id,
        )
        isolated_services.tasks.append_logs(task.id, "stdout", ["deployment line"])
        client = client_stack.enter_context(TestClient(create_app(isolated_services)))
        admin_token, _record = administrator_credential(isolated_services.context, "root")

        response = client.get(
            "/api/v1/logs",
            params={
                "workspace_id": workspace_id,
                "object_type": "deployment",
                "object_id": deployment.id,
            },
            headers=_auth(admin_token),
        )

        assert response.status_code == 200
        payload = response.json()
        assert [item["message"] for item in payload["data"]] == ["deployment line"]
        assert payload["data"][0]["deployment_id"] == deployment.id


def _container_log_data(
    *,
    message: str,
    entry_kind: str = "output",
    workspace_id: str = "workspace-1",
    stub_id: str = "stub-1",
    app_id: str = "app-1",
    task_id: str = "task-1",
    container_id: str = "container-1",
    machine_id: str = "machine-1",
    worker_id: str = "worker-1",
) -> dict[str, JsonValue]:
    return {
        "workspace_id": workspace_id,
        "stub_id": stub_id,
        "app_id": app_id,
        "task_id": task_id,
        "container_id": container_id,
        "machine_id": machine_id,
        "worker_id": worker_id,
        "message": message,
        "stream": "stdout",
        "entry_kind": entry_kind,
        "timestamp": utc_now().isoformat(),
    }


def _append_container_log(
    repo: RedisEventStreamRepository,
    *,
    message: str,
    workspace_id: str = "workspace-1",
    stub_id: str = "stub-1",
    app_id: str = "app-1",
    task_id: str = "task-1",
    container_id: str = "container-1",
    machine_id: str = "machine-1",
    worker_id: str = "worker-1",
) -> None:
    repo.append_event(
        EventRecordType.ContainerLog,
        _container_log_data(
            message=message,
            workspace_id=workspace_id,
            stub_id=stub_id,
            app_id=app_id,
            task_id=task_id,
            container_id=container_id,
            machine_id=machine_id,
            worker_id=worker_id,
        ),
        event_id=f"event-{message.replace(' ', '-')}",
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
