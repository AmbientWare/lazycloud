from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from uuid import NAMESPACE_URL, uuid5

import pytest
import uvicorn
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubRecord
from database.records.endpoint_dispatch import EndpointDispatchStateRecord
from database.repositories.endpoint_dispatch import EndpointDispatchRepository
from database.repositories.execution import TaskRepository
from database.repositories.orchestration import ContainerRepository
from execution.endpoints.dispatch import (
    DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    AsyncEndpointInstanceDispatcher,
    AsyncEndpointRequestDispatcher,
    AsyncEndpointResponseStream,
    EndpointDispatchRecord,
    EndpointDispatchStatus,
    EndpointDispatchTarget,
)
from execution.endpoints.service import (
    EndpointControlService,
    EndpointDispatchStateRepository,
    EndpointWebSocketDispatchRejected,
)
from fastapi.testclient import TestClient
from gateway.container_readiness import AsyncRedisContainerReadiness
from identity.auth import AuthService
from networking.async_http import AsyncBackendHttpClient
from runner.serve import EndpointServeRunner, RunnerASGIApplication
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.fleet import SchedulerContainerStatus
from scheduler.state import (
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
    SchedulerWorkerRequest,
)
from shared.container_requests import (
    CONTAINER_INNER_PORT,
    WorkerContainerRequestPayload,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.endpoints import EndpointForwardRequest
from shared.http.gateway_tasks import AppendTaskLogRequest, AppendTaskLogResponse
from shared.http_transport import HttpChannel
from shared.tasks import RetryPolicy, Task, TaskStatus
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect
from tests.http_server import running_http_server
from tests.metric_helpers import metric_value
from tests.real_redis import RealRedisActors
from tests.releases import assign_runtime
from tests.scheduler_composition import services_with_redis_container_control

pytestmark = pytest.mark.usefixtures("isolated_imports")

# Container ids are UUID columns in production, so a fabricated name would fail
# validation rather than exercise dispatch.
_STREAMING_ASGI_CONTAINER_ID = str(uuid5(NAMESPACE_URL, "lazycloud:test:running-streaming-asgi"))
_HEARTBEAT_CONTAINER_ID = str(uuid5(NAMESPACE_URL, "lazycloud:test:running-heartbeat"))
_WARM_CONTAINER_ID = str(uuid5(NAMESPACE_URL, "lazycloud:test:warm-container"))


def test_endpoint_runner_records_handler_failure_on_request_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler_file = tmp_path / "failing_endpoint.py"
    handler_file.write_text(
        "def fail():\n    raise RuntimeError('intentional endpoint failure')\n",
        encoding="utf-8",
    )
    requests: list[AppendTaskLogRequest] = []

    def record_log(
        _channel: HttpChannel,
        path: str,
        payload: dict[str, str] | None = None,
    ) -> dict[str, str]:
        assert path == "/gateway/tasks/log"
        request = AppendTaskLogRequest.model_validate(payload)
        requests.append(request)
        return AppendTaskLogResponse(task_id=request.task_id).model_dump(mode="json")

    monkeypatch.setattr(HttpChannel, "post", record_log)
    runner = EndpointServeRunner(
        handler_ref=f"{handler_file}:fail",
        endpoint="http://gateway.internal:9000",
        token="leased-token",
    )

    response = runner.handle(
        EndpointForwardRequest(
            stub_id="stub-endpoint",
            method="POST",
            headers={"x-task-id": ["task-failure"]},
            body=b"{}",
        )
    )

    assert response.status_code == 500
    assert b"intentional endpoint failure" in response.body
    assert len(requests) == 1
    assert requests[0].task_id == "task-failure"
    assert requests[0].stream == "stderr"
    assert "Traceback" in requests[0].message
    assert "RuntimeError: intentional endpoint failure" in requests[0].message
    assert "RuntimeError: intentional endpoint failure" in capsys.readouterr().err


def test_asgi_runner_streams_http_and_proxies_websocket_subprotocol(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    handler_file = tmp_path / "asgi_streaming.py"
    handler_file.write_text(
        """
import asyncio


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})
        return
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept", "subprotocol": "events.v1"})
        message = await receive()
        await send({"type": "websocket.send", "text": "echo:" + message["text"]})
        await send({"type": "websocket.close", "code": 4001, "reason": "stream complete"})
        return
    message = await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain"), (b"x-body", message["body"])],
        }
    )
    await send({"type": "http.response.body", "body": b"first", "more_body": True})
    await asyncio.sleep(0.2)
    await send({"type": "http.response.body", "body": b"second"})
""".strip(),
        encoding="utf-8",
    )
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="streaming-asgi",
            kind=DeploymentKind.Asgi,
            handler=f"{handler_file}:app",
        )
    )
    stub = _stub_for_deployment(isolated_services, deployment.id)
    _set_endpoint_dispatch_limits(isolated_services, stub, timeout_seconds=2)

    with _serve_asgi_handler(f"{handler_file}:app") as served:
        _record_dispatch_container(
            isolated_services,
            stub,
            _STREAMING_ASGI_CONTAINER_ID,
        )
        containers = _EndpointContainers(
            states=[
                SchedulerContainerState(
                    container_id=_STREAMING_ASGI_CONTAINER_ID,
                    stub_id=stub.id,
                    workspace_id=stub.workspace_id,
                    status=SchedulerContainerStatus.Running,
                )
            ],
            addresses={_STREAMING_ASGI_CONTAINER_ID: served.address},
        )
        service = _endpoint_service(
            isolated_services,
            containers,
            readiness=_readiness(isolated_services),
        )

        with TestClient(create_app(isolated_services, endpoint_service=service)) as client:
            response = client.post(
                f"/api/v1/asgi/id/{stub.id}/events",
                headers=_auth_headers(isolated_services),
                content=b"through-api",
            )
            assert response.status_code == 200
            assert response.content == b"firstsecond"
            assert response.headers["x-body"] == "through-api"
            assert response.headers["x-task-id"]

            with client.websocket_connect(
                f"/api/v1/asgi/id/{stub.id}/events",
                headers=_auth_headers(isolated_services),
                subprotocols=["events.v1"],
            ) as websocket:
                assert websocket.accepted_subprotocol == "events.v1"
                websocket.send_text("hello")
                assert websocket.receive_text() == "echo:hello"
                with pytest.raises(WebSocketDisconnect) as closed:
                    websocket.receive_text()
                assert closed.value.code == 4001
                assert closed.value.reason == "stream complete"


@pytest.mark.anyio
async def test_asgi_websocket_dispatch_session_heartbeats_and_finishes(
    async_services: ApiServices,
) -> None:
    deployment = async_services.deployments.deploy(
        DeploymentSpec(
            name="realtime-heartbeat",
            kind=DeploymentKind.Asgi,
            handler="module:app",
        )
    )
    stub = _stub_for_deployment(async_services, deployment.id)
    _record_dispatch_container(async_services, stub, _HEARTBEAT_CONTAINER_ID)
    containers = _EndpointContainers(
        states=[
            SchedulerContainerState(
                container_id=_HEARTBEAT_CONTAINER_ID,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
                status=SchedulerContainerStatus.Running,
            )
        ],
        addresses={_HEARTBEAT_CONTAINER_ID: "127.0.0.1:8001"},
    )
    service = _endpoint_service(async_services, containers)

    session = await service.prepare_asgi_websocket(
        EndpointForwardRequest(
            stub_id=stub.id,
            method="GET",
            path="/ws",
            headers={"x-client": ["realtime"]},
        )
    )
    task = async_services.tasks.get(session.task_id)
    before = _dispatch_record(async_services, task).heartbeat_at

    await service.heartbeat_asgi_websocket(session.task_id)
    task = async_services.tasks.get(session.task_id)
    after = _dispatch_record(async_services, task).heartbeat_at
    await service.finish_asgi_websocket(session.task_id)

    finished = async_services.tasks.get(session.task_id)
    assert session.target.container_id == _HEARTBEAT_CONTAINER_ID
    assert session.headers["X-Task-Id"] == [session.task_id]
    assert before is not None
    assert after is not None
    assert after >= before
    assert finished.status is TaskStatus.Complete
    assert _dispatch_record(async_services, finished).status is EndpointDispatchStatus.Complete


@pytest.mark.anyio
async def test_endpoint_service_without_running_container_schedules_warmup(
    async_services: ApiServices,
) -> None:
    scheduler = _RecordingScheduler()
    async_services.containers.scheduler = scheduler
    deployment = async_services.deployments.deploy(
        DeploymentSpec(
            name="cold",
            kind=DeploymentKind.Endpoint,
            handler="module:handler",
        )
    )
    stub = _stub_for_deployment(async_services, deployment.id)
    _set_endpoint_dispatch_limits(async_services, stub, timeout_seconds=0.1)
    service = _endpoint_service(async_services, _EndpointContainers())

    response = await service.forward_endpoint_request(
        EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
    )

    assert response.status_code == 504
    assert b"Timed out waiting for a backend container" in response.body
    assert len(scheduler.requests) == 1
    payload = WorkerContainerRequestPayload.model_validate(scheduler.requests[0].payload)
    assert payload.ports == [CONTAINER_INNER_PORT]
    assert payload.requested_ports == [CONTAINER_INNER_PORT]
    task = async_services.tasks.list()[0]
    dispatch = _dispatch_record(async_services, task)
    assert dispatch.status is EndpointDispatchStatus.Timeout


@pytest.mark.anyio
async def test_endpoint_service_waits_for_warm_capacity_before_dispatch(
    async_services: ApiServices,
    tmp_path: Path,
) -> None:
    handler_file = tmp_path / "delayed_endpoint_handlers.py"
    handler_file.write_text(
        """
def predict():
    return "ready"
""".strip()
    )
    scheduler = _RecordingScheduler()
    async_services.containers.scheduler = scheduler
    deployment = async_services.deployments.deploy(
        DeploymentSpec(
            name="delayed",
            kind=DeploymentKind.Endpoint,
            handler=f"{handler_file}:predict",
        )
    )
    stub = _stub_for_deployment(async_services, deployment.id)
    _set_endpoint_dispatch_limits(async_services, stub, timeout_seconds=1)
    containers = _EndpointContainers()
    service = _endpoint_service(async_services, containers, readiness=_readiness(async_services))
    invocation = asyncio.create_task(
        service.forward_endpoint_request(
            EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
        )
    )
    with _serve_handler(
        f"{handler_file}:predict",
        stub_type=DeploymentKind.Endpoint.value,
    ) as served:
        for _ in range(100):
            if scheduler.requests:
                break
            await asyncio.sleep(0.01)
        assert len(scheduler.requests) == 1
        # The dispatch in flight commits its transaction from this loop, so a
        # sync write here must not block the loop while it waits on that lock.
        await asyncio.to_thread(
            _record_dispatch_container, async_services, stub, _WARM_CONTAINER_ID
        )
        containers.states.append(
            SchedulerContainerState(
                container_id=_WARM_CONTAINER_ID,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
                status=SchedulerContainerStatus.Running,
            )
        )
        containers.addresses[_WARM_CONTAINER_ID] = served.address
        response = await asyncio.wait_for(invocation, timeout=2)

    assert response.status_code == 200
    assert response.body == b"ready"
    task = async_services.tasks.list()[0]
    dispatch = _dispatch_record(async_services, task)
    assert dispatch.status is EndpointDispatchStatus.Complete
    assert dispatch.container_id == _WARM_CONTAINER_ID


@pytest.mark.anyio
async def test_endpoint_retry_requeues_the_relational_dispatch(
    async_services: ApiServices,
) -> None:
    deployment = async_services.deployments.deploy(
        DeploymentSpec(
            name="retry-dispatch",
            kind=DeploymentKind.Endpoint,
            handler="module:handler",
            retry_policy=RetryPolicy(max_attempts=2),
        )
    )
    stub = _stub_for_deployment(async_services, deployment.id)
    _set_endpoint_dispatch_limits(async_services, stub, timeout_seconds=1)
    container_id = str(uuid5(NAMESPACE_URL, "lazycloud:test:retry-container"))
    _record_dispatch_container(async_services, stub, container_id)
    containers = _EndpointContainers(
        states=[
            SchedulerContainerState(
                container_id=container_id,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
                status=SchedulerContainerStatus.Running,
            )
        ],
        addresses={container_id: "127.0.0.1:1"},
    )
    dispatcher = _RetryingEndpointDispatcher(
        containers,
        AsyncBackendHttpClient(),
        _ServingContainers(),
    )

    response = await _endpoint_service(
        async_services,
        containers,
        dispatcher=dispatcher,
    ).forward_endpoint_request(EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}"))

    task = next(task for task in async_services.tasks.list() if task.stub_id == stub.id)
    dispatch = _dispatch_record(async_services, task)
    assert response.status_code == 200
    assert dispatcher.attempts == 2
    assert task.status is TaskStatus.Complete
    assert dispatch.status is EndpointDispatchStatus.Complete
    assert dispatch.attempts == 2


@pytest.mark.anyio
async def test_endpoint_and_asgi_reject_before_creating_runs_when_request_buffer_is_full(
    async_services: ApiServices,
) -> None:
    for kind in (DeploymentKind.Endpoint, DeploymentKind.Asgi):
        deployment = async_services.deployments.deploy(
            DeploymentSpec(
                name=f"busy-{kind.value}",
                kind=kind,
                handler="module:handler",
            )
        )
        stub = _stub_for_deployment(async_services, deployment.id)
        _set_endpoint_dispatch_limits(
            async_services,
            stub,
            timeout_seconds=1,
            max_pending=1,
        )
        existing = _record_active_dispatch(async_services, stub)
        task_ids_before = {task.id for task in async_services.tasks.list()}

        response = await _endpoint_service(
            async_services,
            _EndpointContainers(),
        ).forward_endpoint_request(
            EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
        )

        assert response.status_code == 429
        assert response.headers.get("X-Task-Id") is None
        assert {task.id for task in async_services.tasks.list()} == task_ids_before
        assert (
            _dispatch_record(async_services, existing).status
            is EndpointDispatchStatus.WaitingCapacity
        )
        assert (
            metric_value(
                "endpoint_admission_rejected_total",
                stub_id=stub.id,
                kind=kind.value,
                reason="request_buffer_full",
            )
            == 1
        )


@pytest.mark.anyio
async def test_asgi_websocket_rejects_before_creating_run_when_request_buffer_is_full(
    async_services: ApiServices,
) -> None:
    deployment = async_services.deployments.deploy(
        DeploymentSpec(
            name="busy-websocket",
            kind=DeploymentKind.Asgi,
            handler="module:handler",
        )
    )
    stub = _stub_for_deployment(async_services, deployment.id)
    _set_endpoint_dispatch_limits(async_services, stub, timeout_seconds=1, max_pending=1)
    existing = _record_active_dispatch(async_services, stub)
    task_ids_before = {task.id for task in async_services.tasks.list()}

    with pytest.raises(
        EndpointWebSocketDispatchRejected,
        match="endpoint request buffer is full",
    ) as exc_info:
        await _endpoint_service(
            async_services,
            _EndpointContainers(),
        ).prepare_asgi_websocket(EndpointForwardRequest(stub_id=stub.id, method="GET", path="/ws"))

    assert exc_info.value.status_code == 429
    assert exc_info.value.task_id == ""
    assert {task.id for task in async_services.tasks.list()} == task_ids_before
    assert (
        _dispatch_record(async_services, existing).status is EndpointDispatchStatus.WaitingCapacity
    )
    assert (
        metric_value(
            "endpoint_admission_rejected_total",
            stub_id=stub.id,
            kind=DeploymentKind.Asgi.value,
            reason="request_buffer_full",
        )
        == 1
    )


@pytest.mark.anyio
async def test_endpoint_service_ignores_stale_dispatch_records_for_backpressure(
    async_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    services = services_with_redis_container_control(
        async_services,
        real_redis_actors.client(),
    )
    deployment = services.deployments.deploy(
        DeploymentSpec(
            name="stale-busy",
            kind=DeploymentKind.Endpoint,
            handler="module:handler",
        )
    )
    stub = _stub_for_deployment(services, deployment.id)
    _set_endpoint_dispatch_limits(services, stub, timeout_seconds=0.1, max_pending=1)
    existing = services.tasks.create(
        "endpoint-stale-busy",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        kwargs={},
    )
    stale_at = existing.created_at - timedelta(seconds=5)
    _insert_dispatch_state(
        services,
        existing,
        stub=stub,
        status=EndpointDispatchStatus.WaitingCapacity,
        at=stale_at,
        wait_timeout_seconds=0.1,
        max_pending_requests=1,
        max_inflight_per_container=1,
    )

    response = await _endpoint_service(
        services,
        _EndpointContainers(),
    ).forward_endpoint_request(EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}"))

    assert response.status_code == 504
    newest_task = services.tasks.list()[0]
    assert _dispatch_record(services, newest_task).status is EndpointDispatchStatus.Timeout


@pytest.mark.anyio
async def test_endpoint_service_cancelled_request_stops_waiting_for_capacity(
    async_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    services = services_with_redis_container_control(
        async_services,
        real_redis_actors.client(),
    )
    deployment = services.deployments.deploy(
        DeploymentSpec(
            name="cancel-wait",
            kind=DeploymentKind.Endpoint,
            handler="module:handler",
        )
    )
    stub = _stub_for_deployment(services, deployment.id)
    _set_endpoint_dispatch_limits(services, stub, timeout_seconds=1)
    containers = _EndpointContainers()
    dispatcher = _CancellingEndpointDispatcher(containers, services)
    service = _endpoint_service(
        services,
        containers,
        dispatcher=dispatcher,
    )

    response = await service.forward_endpoint_request(
        EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
    )

    assert response.status_code == 499
    task = dispatcher.cancelled_task
    assert task is not None
    cancelled = services.tasks.get(task.id)
    assert cancelled.status is TaskStatus.Cancelled
    assert _dispatch_record(services, cancelled).status is EndpointDispatchStatus.Cancelled


def _stub_for_deployment(services: ApiServices, deployment_id: str) -> StubRecord:
    matches = [
        stub
        for stub in ControlPlaneService(services.context).list_stubs()
        if stub.deployment_id == deployment_id
    ]
    assert len(matches) == 1
    return matches[0]


def _dispatch_record(services: ApiServices, task: Task) -> EndpointDispatchRecord:
    return EndpointDispatchStateRepository(services).for_task(task)


def _record_active_dispatch(services: ApiServices, stub: StubRecord) -> Task:
    task = services.tasks.create(
        f"{stub.kind.value}-{stub.name}",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        kwargs={},
    )
    _insert_dispatch_state(
        services,
        task,
        stub=stub,
        status=EndpointDispatchStatus.Queued,
        at=task.created_at,
        wait_timeout_seconds=1,
        max_pending_requests=1,
        max_inflight_per_container=1,
    )
    EndpointDispatchStateRepository(services).transition(
        task,
        EndpointDispatchStatus.WaitingCapacity,
    )
    return task


def _insert_dispatch_state(
    services: ApiServices,
    task: Task,
    *,
    stub: StubRecord,
    status: EndpointDispatchStatus,
    at: datetime,
    wait_timeout_seconds: float,
    max_pending_requests: int,
    max_inflight_per_container: int,
) -> None:
    with services.context.database.session() as session:
        EndpointDispatchRepository(session).create(
            EndpointDispatchStateRecord(
                task_id=task.id,
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                container_id=None,
                method="POST",
                path="/",
                status=status.value,
                wait_timeout_seconds=wait_timeout_seconds,
                max_pending_requests=max_pending_requests,
                max_inflight_per_container=max_inflight_per_container,
                attempts=0,
                enqueued_at=at,
                started_at=None,
                heartbeat_at=at,
                expires_at=at + timedelta(seconds=max(wait_timeout_seconds, 1.0)),
                finished_at=None,
                error=None,
            )
        )


def _record_dispatch_container(
    services: ApiServices,
    stub: StubRecord,
    container_id: str,
) -> ContainerRecord:
    with services.context.database.session() as session:
        record = ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name=f"endpoint-{container_id}",
                image="endpoint-image",
                command=["python", "-m", "runner.serve"],
                workspace_id=stub.workspace_id,
                app_id=stub.app_id,
                stub_id=stub.id,
                status=ContainerStatus.Running,
            )
        )
    assign_runtime(services.containers, services.scheduler_workers, container_id)
    return record


def _set_endpoint_dispatch_limits(
    services: ApiServices,
    stub: StubRecord,
    *,
    timeout_seconds: float,
    max_pending: int = 10,
    concurrency: int = 1,
) -> None:
    ControlPlaneService(services.context).update_stub_config(
        stub.id,
        fields={
            "runtime.timeout_seconds": timeout_seconds,
            "runtime.concurrency": concurrency,
            "max_pending_tasks": max_pending,
        },
    )


@dataclass(frozen=True, slots=True)
class _ServedEndpoint:
    address: str


@contextmanager
def _serve_handler(handler_ref: str, *, stub_type: str) -> Iterator[_ServedEndpoint]:
    runner = EndpointServeRunner(
        handler_ref=handler_ref, stub_type=stub_type, host="127.0.0.1", port=0
    )
    server = runner.create_server()
    with running_http_server(server):
        yield _ServedEndpoint(f"127.0.0.1:{server.server_port}")


@dataclass(frozen=True, slots=True)
class _ServingContainers:
    """For the tests that never stand a backend up.

    They cover dispatch bookkeeping — queue admission, heartbeats, cancellation —
    against an address nothing listens on, so a real probe would correctly find
    nothing serving and time them out. The probe itself is exercised by the tests
    that do serve a runner, which use `_readiness`.
    """

    async def is_ready(
        self,
        *,
        container_id: str,
        stub_id: str,
        address: str,
        route_id: str,
        port: int,
        health_path: str = "",
    ) -> bool:
        _ = container_id, stub_id, address, route_id, port, health_path
        return True


def _readiness(services: ApiServices) -> AsyncRedisContainerReadiness:
    """The production probe, dialing the served runner directly.

    These addresses are plain host:port rather than backend routes, so the
    gateway client needs no resolver — and the runner answers `/health` itself,
    which is exactly what the probe asks in production.
    """

    client = AsyncBackendHttpClient()
    return AsyncRedisContainerReadiness(services.require_async_io().redis, client)


def _endpoint_service(
    services: ApiServices,
    containers: _EndpointContainers,
    *,
    readiness: _ServingContainers | AsyncRedisContainerReadiness | None = None,
    dispatcher: AsyncEndpointRequestDispatcher | None = None,
) -> EndpointControlService:
    async_io = services.require_async_io()
    return EndpointControlService(
        services,
        async_database=async_io.database,
        async_dispatcher=(
            dispatcher
            or AsyncEndpointInstanceDispatcher(
                containers,
                AsyncBackendHttpClient(),
                readiness or _ServingContainers(),
            )
        ),
    )


@dataclass(slots=True)
class _ServedASGI:
    server: uvicorn.Server
    thread: threading.Thread
    listener: socket.socket

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.listener.getsockname()[1]}"

    def __enter__(self) -> _ServedASGI:
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            if not self.thread.is_alive():
                break
            time.sleep(0.01)
        self.server.should_exit = True
        self.thread.join(timeout=2)
        self.listener.close()
        raise AssertionError("ASGI server did not finish startup")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, tb
        self.server.should_exit = True
        self.thread.join(timeout=2)
        self.listener.close()
        assert not self.thread.is_alive(), "ASGI server did not stop"


def _serve_asgi_handler(handler_ref: str) -> _ServedASGI:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    runner = EndpointServeRunner(
        handler_ref=handler_ref,
        stub_type=DeploymentKind.Asgi.value,
        host="127.0.0.1",
        port=port,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            RunnerASGIApplication(runner),
            host=runner.host,
            port=runner.port,
            log_level="error",
        )
    )
    return _ServedASGI(
        server=server,
        thread=threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True),
        listener=listener,
    )


@dataclass
class _EndpointContainers:
    states: list[SchedulerContainerState] = field(default_factory=list)
    addresses: dict[str, str] = field(default_factory=dict)
    address_maps: dict[str, SchedulerContainerAddressMap] = field(default_factory=dict)

    async def list_by_stub(self, stub_id: str) -> list[SchedulerContainerState]:
        return [state for state in self.states if state.stub_id == stub_id]

    async def get_container_address(
        self,
        container_id: str,
    ) -> SchedulerContainerAddress | None:
        address = self.addresses.get(container_id, "")
        if not address:
            return None
        return SchedulerContainerAddress(container_id=container_id, address=address)

    async def get_container_address_maps(
        self,
        container_ids: Sequence[str],
    ) -> dict[str, SchedulerContainerAddressMap]:
        return {
            container_id: self.address_maps.get(
                container_id,
                SchedulerContainerAddressMap(container_id=container_id),
            )
            for container_id in container_ids
        }


class _CancellingEndpointDispatcher(AsyncEndpointInstanceDispatcher):
    def __init__(self, containers: _EndpointContainers, runtime: ApiServices) -> None:
        super().__init__(containers, AsyncBackendHttpClient(), _ServingContainers())
        self.runtime = runtime
        self.cancelled_task: Task | None = None

    async def select_target(
        self,
        stub_id: str,
        *,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = 1,
        excluded_container_ids: frozenset[str] | set[str] = frozenset(),
    ) -> EndpointDispatchTarget | None:
        _ = container_loads, max_inflight_per_container
        if self.cancelled_task is None:

            def endpoint_tasks(session: Session) -> list[Task]:
                return TaskRepository(session).list_across_workspaces()

            tasks = await self.runtime.require_async_io().database.run_transaction(endpoint_tasks)
            self.cancelled_task = next(task for task in tasks if task.stub_id == stub_id)
            await self.runtime.tasks.transition_async(
                self.cancelled_task,
                TaskStatus.Cancelled,
            )
        return await super().select_target(
            stub_id,
            container_loads=container_loads,
            max_inflight_per_container=max_inflight_per_container,
            excluded_container_ids=excluded_container_ids,
        )


@dataclass(slots=True)
class _StaticEndpointResponse:
    status_code: int
    body: bytes
    headers: dict[str, list[str]] = field(default_factory=dict)

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        yield self.body

    async def close(self) -> None:
        return


class _RetryingEndpointDispatcher(AsyncEndpointInstanceDispatcher):
    attempts = 0

    async def open_http_stream(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> AsyncEndpointResponseStream:
        del target, request, timeout_seconds
        self.attempts += 1
        if self.attempts == 1:
            return _StaticEndpointResponse(status_code=500, body=b"retry")
        return _StaticEndpointResponse(status_code=200, body=b"complete")


@dataclass
class _RecordingScheduler:
    requests: list[SchedulerWorkerRequest] = field(default_factory=list)

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        _ = ready_at
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
        )


def _auth_headers(
    services: ApiServices,
    *,
    workspace: str = "default",
) -> dict[str, str]:
    raw_token, _record = AuthService(services.context).create_token(
        f"endpoint-lifecycle-{workspace}",
        scopes=["read", "write"],
        workspace_id=workspace,
    )
    return {"Authorization": f"Bearer {raw_token}"}


def test_health_probe_reaches_the_container_without_opening_an_invocation(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    """A probe asks whether the workload could run something, and runs nothing.

    Every other public path here creates a task and meters it. An uptime check
    left pointing at this URL would otherwise bill the workspace once per poll
    for work no handler ever saw, so the absence of both records is the contract.
    """

    handler_file = tmp_path / "asgi_health.py"
    handler_file.write_text(
        """
async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})
        return
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"handler ran"})
""".strip(),
        encoding="utf-8",
    )
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="health-probe-asgi",
            kind=DeploymentKind.Asgi,
            handler=f"{handler_file}:app",
        )
    )
    stub = _stub_for_deployment(isolated_services, deployment.id)
    _set_endpoint_dispatch_limits(isolated_services, stub, timeout_seconds=2)
    container_id = "00000000-0000-4000-8000-0000000003b1"

    with _serve_asgi_handler(f"{handler_file}:app") as served:
        containers = _EndpointContainers(
            states=[
                SchedulerContainerState(
                    container_id=container_id,
                    stub_id=stub.id,
                    workspace_id=stub.workspace_id,
                    status=SchedulerContainerStatus.Running,
                )
            ],
            addresses={container_id: served.address},
        )
        service = _endpoint_service(
            isolated_services,
            containers,
            readiness=_readiness(isolated_services),
        )
        task_ids_before = {task.id for task in isolated_services.tasks.list()}
        usage_before = len(isolated_services.usage.list(workspace_id=stub.workspace_id))

        with TestClient(create_app(isolated_services, endpoint_service=service)) as client:
            response = client.get(
                f"/api/v1/asgi/id/{stub.id}/health",
                headers=_auth_headers(isolated_services),
            )

            assert response.status_code == 200
            # The runner answers this itself, so reaching it proves the request
            # was forwarded to the container rather than answered in the API.
            assert response.content == b"ok"
            assert {task.id for task in isolated_services.tasks.list()} == task_ids_before
            assert len(isolated_services.usage.list(workspace_id=stub.workspace_id)) == usage_before
