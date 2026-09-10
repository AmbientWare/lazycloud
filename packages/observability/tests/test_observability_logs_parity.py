from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from observability.stream_state import (
    AsyncRedisEventStreamRepository,
    RedisEventStreamRepository,
    log_record_from_redis,
)
from pydantic import JsonValue
from shared.deployment_records import DeploymentSpec
from shared.errors import ExpiredCursorError
from shared.http.observability import LogRecord
from shared.realtime.contracts import EventRecordType, create_cloud_event_record
from shared.realtime.streams import LogStreamQuery
from shared.timestamps import utc_now
from tests.real_redis import RealRedisActors
from tests.workspaces import administrator_credential


@pytest.fixture
async def async_io(isolated_services: ApiServices) -> AsyncIterator[ApiAsyncIo]:
    io = isolated_services.require_async_io()
    await io.start()
    try:
        yield io
    finally:
        await io.close()


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


@pytest.mark.anyio
async def test_redis_log_stream_resumes_by_sequence_through_skipped_records(
    async_io: ApiAsyncIo,
    real_redis_actors: RealRedisActors,
) -> None:
    repo = RedisEventStreamRepository(real_redis_actors.client())
    _append_container_log(repo, message="first", task_id="task-1")
    cursor = repo.read_logs(LogStreamQuery(workspace_id="workspace-1", task_id="task-1"))[
        -1
    ].entry_id
    _append_container_log(repo, message="skip", task_id="other-task")
    _append_container_log(repo, message="second", task_id="task-1")

    followed = await AsyncRedisEventStreamRepository(async_io.redis).follow_logs(
        async_io.realtime,
        LogStreamQuery(
            workspace_id="workspace-1",
            task_id="task-1",
            cursor=cursor,
        ),
        max_events=1,
        heartbeat_seconds=1.0,
    )

    assert [
        log_record_from_redis(record).message async for record in followed if record is not None
    ] == ["second"]


@pytest.mark.anyio
async def test_log_follow_caps_replay_and_continues_with_new_output(
    async_io: ApiAsyncIo,
    real_redis_actors: RealRedisActors,
) -> None:
    repo = RedisEventStreamRepository(real_redis_actors.client())
    repo.append_container_log_batch(
        container_id="container-1",
        capture_id="large-output",
        first_sequence=0,
        events=tuple(
            create_cloud_event_record(
                EventRecordType.ContainerLog,
                _container_log_data(message=f"line-{index}"),
                event_id=f"large-{index}",
            )
            for index in range(2_000)
        ),
    )
    followed = await AsyncRedisEventStreamRepository(async_io.redis).follow_logs(
        async_io.realtime,
        LogStreamQuery(workspace_id="workspace-1", task_id="task-1", limit=200),
        max_events=201,
        heartbeat_seconds=0.1,
    )
    _append_container_log(repo, message="live")
    messages = [log_record_from_redis(record).message async for record in followed if record]
    assert messages == [*(f"line-{index}" for index in range(1_800, 2_000)), "live"]


@pytest.mark.anyio
async def test_task_log_write_reaches_live_stream_with_durable_identity(
    async_io: ApiAsyncIo,
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    task = isolated_services.tasks.create("live-output", workspace_id=workspace_id)
    followed = await AsyncRedisEventStreamRepository(async_io.redis).follow_logs(
        async_io.realtime,
        LogStreamQuery(workspace_id=workspace_id, task_id=task.id),
        max_events=1,
        heartbeat_seconds=0.1,
    )
    isolated_services.tasks.append_log(task.id, "stdout", "new output\n")
    query = LogStreamQuery(workspace_id=workspace_id, task_id=task.id)
    captured = isolated_services.tasks.log_streams.read_logs(query)
    assert len(captured) == 1
    live: list[LogRecord] = []
    try:
        for _ in range(10):
            record = await anext(followed)
            print("task log stream", async_io.realtime.status(), "record", record is not None)
            if record is not None:
                live.append(log_record_from_redis(record))
                break
    finally:
        await followed.aclose()
    stored = isolated_services.tasks.logs(task.id)
    assert [(record.id, record.message, record.task_id) for record in live] == [
        (stored[0].id, "new output", task.id)
    ]


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
    client_stack: ExitStack,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    task = isolated_services.tasks.create("durable-log", workspace_id=workspace_id)
    isolated_services.tasks.append_log(task.id, "stdout", "needle durable")
    redis = real_redis_actors.client()
    repo = RedisEventStreamRepository(redis)
    _append_container_log(repo, message="needle first", workspace_id=workspace_id)
    first_cursor = repo.read_logs(LogStreamQuery(workspace_id=workspace_id))[-1].entry_id
    _append_container_log(repo, message="needle second", workspace_id=workspace_id)
    second_cursor = repo.read_logs(LogStreamQuery(workspace_id=workspace_id))[-1].entry_id
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    admin_token, _record = administrator_credential(isolated_services.context, "root")

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
    assert [item["message"] for item in payload["data"]] == ["needle durable"]
    assert payload["data"][0]["task_id"] == task.id
    assert stream.status_code == 200
    assert f"id: {second_cursor}" in stream.text
    assert "needle second" in stream.text


def test_api_deployment_logs_resolve_deployment_to_owned_stream(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
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
    isolated_services.tasks.append_log(task.id, "stdout", "deployment line")
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
