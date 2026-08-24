from __future__ import annotations

from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from coordination.redis_client import RedisClient
from fastapi.testclient import TestClient
from observability.stream_state import RedisEventStreamRepository, log_record_from_redis
from pydantic import JsonValue
from shared.deployment_records import DeploymentSpec
from shared.errors import ExpiredCursorError
from shared.realtime.contracts import EventRecordType, create_cloud_event_record
from shared.realtime.streams import LogStreamQuery
from tests.real_redis import RealRedisActors
from tests.service_fixtures import administrator_credential


def test_redis_log_repository_applies_filter_combinations(
    real_redis_actors: RealRedisActors,
) -> None:
    repo = RedisEventStreamRepository(real_redis_actors.client())
    _append_container_log(repo, message="needle match")
    _append_container_log(repo, message="wrong stub", stub_id="other-stub")
    _append_container_log(repo, message="wrong worker", worker_id="other-worker")

    records = repo.read_logs(
        LogStreamQuery(
            workspace_id="workspace-1",
            object_type="container",
            object_id="container-1",
            stub_id="stub-1",
            app_id="app-1",
            task_id="task-1",
            machine_id="machine-1",
            worker_id="worker-1",
            query="needle",
        )
    )
    logs = [log_record_from_redis(record) for record in records]

    assert [log.message for log in logs] == ["needle match"]
    assert logs[0].container_id == "container-1"
    assert logs[0].stub_id == "stub-1"
    assert logs[0].app_id == "app-1"
    assert logs[0].machine_id == "machine-1"
    assert logs[0].worker_id == "worker-1"


def test_redis_log_stream_resumes_by_sequence_through_skipped_records(
    real_redis_actors: RealRedisActors,
) -> None:
    repo = RedisEventStreamRepository(real_redis_actors.client())
    _append_container_log(repo, message="first", task_id="task-1")
    cursor = repo.read_logs(LogStreamQuery(workspace_id="workspace-1", task_id="task-1"))[
        -1
    ].entry_id
    _append_container_log(repo, message="skip", task_id="other-task")
    _append_container_log(repo, message="second", task_id="task-1")

    followed = list(
        repo.stream_logs(
            LogStreamQuery(
                workspace_id="workspace-1",
                task_id="task-1",
                cursor=cursor,
            ),
            block_milliseconds=1,
            max_events=1,
        )
    )

    assert [log_record_from_redis(record).message for record in followed] == ["second"]


def test_redis_log_read_honors_clamp(real_redis_actors: RealRedisActors) -> None:
    repo = RedisEventStreamRepository(real_redis_actors.client())
    _append_container_log(repo, message="retained")

    clamped = repo.read_logs(LogStreamQuery(workspace_id="workspace-1", seq_num=0, clamp=True))

    with pytest.raises(ExpiredCursorError):
        repo.read_logs(LogStreamQuery(workspace_id="workspace-1", seq_num=0, clamp=False))
    assert [log_record_from_redis(record).message for record in clamped] == ["retained"]


def test_redis_log_batch_reads_back_in_capture_order(
    real_redis_actors: RealRedisActors,
) -> None:
    # One Lua script stamps every entry it appends with the same millisecond, so a
    # capture longer than ten lines is where entry-ID ordering stops agreeing with
    # text ordering and a reader can hand the user a scrambled page.
    repo = RedisEventStreamRepository(real_redis_actors.client())
    messages = tuple(f"line-{index}" for index in range(24))
    repo.append_container_log_batch(
        container_id="container-1",
        capture_id="capture-1",
        first_sequence=0,
        events=tuple(
            create_cloud_event_record(
                EventRecordType.ContainerLog,
                _container_log_data(message=message),
                event_id=f"batch-{message}",
            )
            for message in messages
        ),
    )

    records = repo.read_logs(LogStreamQuery(workspace_id="workspace-1"))

    assert [log_record_from_redis(record).message for record in records] == list(messages)


def test_capture_barriers_never_reach_a_reader(
    real_redis_actors: RealRedisActors,
) -> None:
    """A flush is the batch boundary the ingest cursor needs, not a line anyone wrote.

    It carries no message, so surfacing it renders as a blank line between real
    output. Dropped stays visible in the same read: it is the only account a reader
    gets of output the worker could not deliver.
    """
    repo = RedisEventStreamRepository(real_redis_actors.client())
    repo.append_container_log_batch(
        container_id="container-1",
        capture_id="capture-barrier",
        first_sequence=0,
        events=(
            create_cloud_event_record(
                EventRecordType.ContainerLog,
                _container_log_data(message="printed"),
                event_id="barrier-output",
            ),
            create_cloud_event_record(
                EventRecordType.ContainerLog,
                _container_log_data(message="", entry_kind="flush"),
                event_id="barrier-flush",
            ),
            create_cloud_event_record(
                EventRecordType.ContainerLog,
                _container_log_data(message="output dropped", entry_kind="dropped"),
                event_id="barrier-dropped",
            ),
        ),
    )

    records = repo.read_logs(LogStreamQuery(workspace_id="workspace-1"))

    assert [log_record_from_redis(record).message for record in records] == [
        "printed",
        "output dropped",
    ]


def test_redis_event_repository_deletes_only_workspace_streams(
    real_redis_actors: RealRedisActors,
) -> None:
    repo = RedisEventStreamRepository(real_redis_actors.client())
    _append_container_log(repo, message="owned-log")
    _append_container_log(repo, message="other-log", workspace_id="workspace-2")
    repo.append_event(
        EventRecordType.ContainerLifecycle,
        {
            "workspace_id": "workspace-1",
            "stub_id": "stub-1",
            "app_id": "app-1",
            "container_id": "container-1",
        },
        event_id="owned-lifecycle",
    )
    repo.append_event(
        EventRecordType.ContainerLifecycle,
        {
            "workspace_id": "workspace-2",
            "stub_id": "stub-2",
            "app_id": "app-2",
            "container_id": "container-2",
        },
        event_id="other-lifecycle",
    )

    deleted = repo.delete_workspace("workspace-1")

    assert deleted > 0
    remaining = set(repo.redis.scan(f"{repo.redis.key_prefix}:*"))
    assert all("workspace-1" not in key for key in remaining)
    assert any("workspace-2" in key for key in remaining)


def test_api_log_history_and_stream_support_filters_wait_and_resume(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    request: pytest.FixtureRequest,
    client_stack: ExitStack,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    redis = real_redis_actors.client()
    services = _services_with_redis(isolated_services, redis, request)
    repo = RedisEventStreamRepository(redis)
    _append_container_log(repo, message="needle first", workspace_id=workspace_id)
    first_cursor = repo.read_logs(LogStreamQuery(workspace_id=workspace_id))[-1].entry_id
    _append_container_log(repo, message="needle second", workspace_id=workspace_id)
    second_cursor = repo.read_logs(LogStreamQuery(workspace_id=workspace_id))[-1].entry_id
    client = client_stack.enter_context(TestClient(create_app(services)))
    admin_token, _record = administrator_credential(isolated_services, "root")

    history = client.get(
        f"/api/v1/logs"
        f"?workspace_id={workspace_id}"
        "&object_type=container&object_id=container-1&stub_id=stub-1&app_id=app-1"
        "&machine_id=machine-1&worker_id=worker-1&query=second",
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
    assert [item["message"] for item in payload["data"]] == ["needle second"]
    assert payload["data"][0]["container_id"] == "container-1"
    assert stream.status_code == 200
    assert f"id: {second_cursor}" in stream.text
    assert "needle second" in stream.text


def test_api_deployment_logs_resolve_deployment_to_owned_stream(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    request: pytest.FixtureRequest,
    client_stack: ExitStack,
) -> None:
    redis = real_redis_actors.client()
    services = _services_with_redis(isolated_services, redis, request)
    deployment = services.deployments.deploy(
        DeploymentSpec(name="deployment-logs", handler="pkg.module:handler")
    )
    assert deployment.app_id
    assert deployment.stub_id
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    _append_container_log(
        RedisEventStreamRepository(redis),
        message="deployment line",
        workspace_id=workspace_id,
        stub_id=deployment.stub_id,
        app_id=deployment.app_id,
    )
    client = client_stack.enter_context(TestClient(create_app(services)))
    admin_token, _record = administrator_credential(isolated_services, "root")

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
    assert payload["streams"] == [
        f"events/logs/workspaces/{workspace_id}/stubs/{deployment.stub_id}"
    ]


def _services_with_redis(
    isolated_services: ApiServices,
    redis: RedisClient,
    request: pytest.FixtureRequest,
) -> ApiServices:
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        workspace_storage_issuer=isolated_services.workspace_storage_issuer,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=redis,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services


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
        "timestamp": "2026-06-20T10:00:00Z",
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
