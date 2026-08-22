from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from uuid import NAMESPACE_URL, uuid5

import pytest
import uvicorn
import websockets.asyncio.server
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubRecord
from execution.endpoints.dispatch import (
    ENDPOINT_DISPATCH_TASK_KEY,
    EndpointDispatchRecord,
    EndpointDispatchStatus,
    EndpointDispatchTarget,
    EndpointInstanceDispatcher,
)
from execution.endpoints.service import (
    EndpointControlService,
    EndpointWebSocketDispatchRejected,
)
from fastapi.testclient import TestClient
from gateway.container_readiness import RedisContainerReadiness
from gateway.pod_proxy import PodProxyHttpClient
from identity.auth import AuthService
from pydantic import JsonValue
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
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.endpoints import (
    EndpointForwardRequest,
    EndpointForwardResponse,
)
from shared.http.gateway_tasks import AppendTaskLogRequest, AppendTaskLogResponse
from shared.http_transport import HttpChannel
from shared.tasks import Task, TaskStatus
from starlette.websockets import WebSocketDisconnect
from tests.metric_helpers import metric_value
from tests.real_redis import RealRedisActors
from tests.scheduler_composition import services_with_redis_container_control
from websockets.asyncio.server import ServerConnection

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
        service = EndpointControlService(
            isolated_services,
            dispatcher=EndpointInstanceDispatcher(
                containers, readiness_probe=_readiness(isolated_services)
            ),
        )
        request = EndpointForwardRequest(
            stub_id=stub.id,
            method="POST",
            path="/events",
            body=b"payload",
        )
        session = service.prepare_asgi_http(request)
        stream = service.open_asgi_http_stream(session, request)
        chunks = iter(stream.iter_chunks())
        started = time.monotonic()
        first = next(chunks)
        first_elapsed = time.monotonic() - started
        remainder = b"".join(chunks)
        service.finish_asgi_http(
            session.task_id,
            status_code=stream.status_code,
            body_size_bytes=len(first) + len(remainder),
        )
        stream.close()

        assert first == b"first"
        assert first_elapsed < 0.15
        assert remainder == b"second"
        assert stream.headers["x-body"] == ["payload"]
        assert isolated_services.tasks.get(session.task_id).status is TaskStatus.Complete

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


def test_asgi_websocket_dispatch_session_heartbeats_and_finishes(
    isolated_services: ApiServices,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="realtime-heartbeat",
            kind=DeploymentKind.Asgi,
            handler="module:app",
        )
    )
    stub = _stub_for_deployment(isolated_services, deployment.id)
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
    service = EndpointControlService(
        isolated_services,
        dispatcher=EndpointInstanceDispatcher(containers, readiness_probe=_ServingContainers()),
    )

    session = service.prepare_asgi_websocket(
        EndpointForwardRequest(
            stub_id=stub.id,
            method="GET",
            path="/ws",
            headers={"x-client": ["realtime"]},
        )
    )
    task = isolated_services.tasks.get(session.task_id)
    before = _dispatch_record(task).heartbeat_at

    service.heartbeat_asgi_websocket(session.task_id)
    task = isolated_services.tasks.get(session.task_id)
    after = _dispatch_record(task).heartbeat_at
    service.finish_asgi_websocket(session.task_id)

    finished = isolated_services.tasks.get(session.task_id)
    assert session.target.container_id == _HEARTBEAT_CONTAINER_ID
    assert session.headers["X-Task-Id"] == [session.task_id]
    assert before is not None
    assert after is not None
    assert after >= before
    assert finished.status is TaskStatus.Complete
    assert _dispatch_record(finished).status is EndpointDispatchStatus.Complete


def test_endpoint_service_without_running_container_schedules_warmup(
    isolated_services: ApiServices,
) -> None:
    scheduler = _RecordingScheduler()
    isolated_services.containers.scheduler = scheduler
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="cold",
            kind=DeploymentKind.Endpoint,
            handler="module:handler",
        )
    )
    stub = _stub_for_deployment(isolated_services, deployment.id)
    _set_endpoint_dispatch_limits(isolated_services, stub, timeout_seconds=0.1)
    service = EndpointControlService(
        isolated_services,
        dispatcher=EndpointInstanceDispatcher(
            _EndpointContainers(), readiness_probe=_ServingContainers()
        ),
    )

    response = service.forward_endpoint_request(
        EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
    )

    assert response.status_code == 504
    assert b"Timed out waiting for a backend container" in response.body
    assert len(scheduler.requests) == 1
    payload = WorkerContainerRequestPayload.model_validate(scheduler.requests[0].payload)
    assert payload.ports == [CONTAINER_INNER_PORT]
    assert payload.requested_ports == [CONTAINER_INNER_PORT]
    task = isolated_services.tasks.list()[0]
    dispatch = _dispatch_record(task)
    assert dispatch.status is EndpointDispatchStatus.Timeout


def test_endpoint_service_waits_for_warm_capacity_before_dispatch(
    isolated_services: ApiServices,
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
    isolated_services.containers.scheduler = scheduler
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="delayed",
            kind=DeploymentKind.Endpoint,
            handler=f"{handler_file}:predict",
        )
    )
    stub = _stub_for_deployment(isolated_services, deployment.id)
    _set_endpoint_dispatch_limits(isolated_services, stub, timeout_seconds=1)
    containers = _EndpointContainers()
    service = EndpointControlService(
        isolated_services,
        dispatcher=EndpointInstanceDispatcher(containers, readiness_probe=_ServingContainers()),
    )
    result: list[EndpointForwardResponse] = []

    def invoke() -> None:
        result.append(
            service.forward_endpoint_request(
                EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
            )
        )

    thread = threading.Thread(target=invoke)
    with _serve_handler(
        f"{handler_file}:predict",
        stub_type=DeploymentKind.Endpoint.value,
        stub_id=stub.id,
    ) as served:
        thread.start()
        _wait_until(lambda: len(scheduler.requests) == 1)
        containers.states.append(
            SchedulerContainerState(
                container_id=_WARM_CONTAINER_ID,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
                status=SchedulerContainerStatus.Running,
            )
        )
        containers.addresses[_WARM_CONTAINER_ID] = served.address
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(result) == 1
    response = result[0]
    assert response.status_code == 200
    assert response.body == b"ready"
    task = isolated_services.tasks.list()[0]
    dispatch = _dispatch_record(task)
    assert dispatch.status is EndpointDispatchStatus.Complete
    assert dispatch.container_id == _WARM_CONTAINER_ID


def test_endpoint_and_asgi_reject_before_creating_runs_when_request_buffer_is_full(
    isolated_services: ApiServices,
) -> None:
    for kind in (DeploymentKind.Endpoint, DeploymentKind.Asgi):
        deployment = isolated_services.deployments.deploy(
            DeploymentSpec(
                name=f"busy-{kind.value}",
                kind=kind,
                handler="module:handler",
            )
        )
        stub = _stub_for_deployment(isolated_services, deployment.id)
        _set_endpoint_dispatch_limits(
            isolated_services,
            stub,
            timeout_seconds=1,
            max_pending=1,
        )
        existing = _record_active_dispatch(isolated_services, stub)
        task_ids_before = {task.id for task in isolated_services.tasks.list()}

        response = EndpointControlService(
            isolated_services,
            dispatcher=EndpointInstanceDispatcher(
                _EndpointContainers(), readiness_probe=_ServingContainers()
            ),
        ).forward_endpoint_request(
            EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
        )

        assert response.status_code == 429
        assert response.headers.get("X-Task-Id") is None
        assert {task.id for task in isolated_services.tasks.list()} == task_ids_before
        assert _dispatch_record(existing).status is EndpointDispatchStatus.WaitingCapacity
        assert (
            metric_value(
                "endpoint_admission_rejected_total",
                stub_id=stub.id,
                kind=kind.value,
                reason="request_buffer_full",
            )
            == 1
        )


def test_asgi_websocket_rejects_before_creating_run_when_request_buffer_is_full(
    isolated_services: ApiServices,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="busy-websocket",
            kind=DeploymentKind.Asgi,
            handler="module:handler",
        )
    )
    stub = _stub_for_deployment(isolated_services, deployment.id)
    _set_endpoint_dispatch_limits(isolated_services, stub, timeout_seconds=1, max_pending=1)
    existing = _record_active_dispatch(isolated_services, stub)
    task_ids_before = {task.id for task in isolated_services.tasks.list()}

    with pytest.raises(
        EndpointWebSocketDispatchRejected,
        match="endpoint request buffer is full",
    ) as exc_info:
        EndpointControlService(
            isolated_services,
            dispatcher=EndpointInstanceDispatcher(
                _EndpointContainers(), readiness_probe=_ServingContainers()
            ),
        ).prepare_asgi_websocket(EndpointForwardRequest(stub_id=stub.id, method="GET", path="/ws"))

    assert exc_info.value.status_code == 429
    assert exc_info.value.task_id == ""
    assert {task.id for task in isolated_services.tasks.list()} == task_ids_before
    assert _dispatch_record(existing).status is EndpointDispatchStatus.WaitingCapacity
    assert (
        metric_value(
            "endpoint_admission_rejected_total",
            stub_id=stub.id,
            kind=DeploymentKind.Asgi.value,
            reason="request_buffer_full",
        )
        == 1
    )


def test_endpoint_service_ignores_stale_dispatch_records_for_backpressure(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    services = services_with_redis_container_control(
        isolated_services,
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
    existing = services.tasks.create("endpoint-stale-busy", kwargs={})
    stale_at = existing.created_at - timedelta(seconds=5)
    existing.kwargs[ENDPOINT_DISPATCH_TASK_KEY] = {
        "task_id": existing.id,
        "stub_id": stub.id,
        "workspace_id": stub.workspace_id,
        "method": "POST",
        "path": "/",
        "status": EndpointDispatchStatus.WaitingCapacity.value,
        "container_id": None,
        "wait_timeout_seconds": 0.1,
        "max_pending_requests": 1,
        "max_inflight_per_container": 1,
        "attempts": 0,
        "enqueued_at": stale_at.isoformat(),
        "started_at": None,
        "heartbeat_at": stale_at.isoformat(),
        "finished_at": None,
        "error": None,
    }
    services.tasks.save(existing)

    response = EndpointControlService(
        services,
        dispatcher=EndpointInstanceDispatcher(
            _EndpointContainers(), readiness_probe=_ServingContainers()
        ),
    ).forward_endpoint_request(EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}"))

    assert response.status_code == 504
    newest_task = services.tasks.list()[0]
    assert _dispatch_record(newest_task).status is EndpointDispatchStatus.Timeout


def test_endpoint_service_cancelled_request_stops_waiting_for_capacity(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    services = services_with_redis_container_control(
        isolated_services,
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
    dispatcher = _CancellingEndpointDispatcher(_EndpointContainers(), services)
    service = EndpointControlService(
        services,
        dispatcher=dispatcher,
    )

    response = service.forward_endpoint_request(
        EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
    )

    assert response.status_code == 499
    task = dispatcher.cancelled_task
    assert task is not None
    cancelled = services.tasks.get(task.id)
    assert cancelled.status is TaskStatus.Cancelled
    assert _dispatch_record(cancelled).status is EndpointDispatchStatus.Cancelled


def _stub_for_deployment(services: ApiServices, deployment_id: str) -> StubRecord:
    matches = [
        stub
        for stub in ControlPlaneService(services.context).list_stubs()
        if stub.deployment_id == deployment_id
    ]
    assert len(matches) == 1
    return matches[0]


def _json_object(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _dispatch_record(task: Task) -> EndpointDispatchRecord:
    return EndpointDispatchRecord.model_validate(task.kwargs[ENDPOINT_DISPATCH_TASK_KEY])


def _record_active_dispatch(services: ApiServices, stub: StubRecord) -> Task:
    task = services.tasks.create(f"{stub.kind.value}-{stub.name}", kwargs={})
    task.kwargs[ENDPOINT_DISPATCH_TASK_KEY] = {
        "task_id": task.id,
        "stub_id": stub.id,
        "workspace_id": stub.workspace_id,
        "method": "POST",
        "path": "/",
        "status": EndpointDispatchStatus.WaitingCapacity.value,
        "container_id": None,
        "wait_timeout_seconds": 1,
        "max_pending_requests": 1,
        "max_inflight_per_container": 1,
        "attempts": 0,
        "enqueued_at": task.created_at.isoformat(),
        "started_at": None,
        "heartbeat_at": task.created_at.isoformat(),
        "finished_at": None,
        "error": None,
    }
    return services.tasks.save(task)


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


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def _wait_for_endpoint_task(
    services: ApiServices,
    *,
    timeout_seconds: float = 1.0,
) -> Task:
    task: Task | None = None

    def task_created() -> bool:
        nonlocal task
        for candidate in services.tasks.list():
            if ENDPOINT_DISPATCH_TASK_KEY in candidate.kwargs:
                task = candidate
                return True
        return False

    _wait_until(task_created, timeout_seconds=timeout_seconds)
    assert task is not None
    return task


@dataclass(slots=True)
class _ServedEndpoint:
    server: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> _ServedEndpoint:
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, tb
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _serve_handler(handler_ref: str, *, stub_type: str, stub_id: str) -> _ServedEndpoint:
    _ = stub_id
    runner = EndpointServeRunner(
        handler_ref=handler_ref,
        stub_type=stub_type,
        host="127.0.0.1",
        port=0,
    )
    server = runner.create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    return _ServedEndpoint(server=server, thread=thread)


@dataclass(frozen=True, slots=True)
class _ServingContainers:
    """For the tests that never stand a backend up.

    They cover dispatch bookkeeping — queue admission, heartbeats, cancellation —
    against an address nothing listens on, so a real probe would correctly find
    nothing serving and time them out. The probe itself is exercised by the tests
    that do serve a runner, which use `_readiness`.
    """

    def is_ready(
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


def _readiness(services: ApiServices) -> RedisContainerReadiness:
    """The production probe, dialing the served runner directly.

    These addresses are plain host:port rather than backend routes, so the
    gateway client needs no resolver — and the runner answers `/health` itself,
    which is exactly what the probe asks in production.
    """

    client = PodProxyHttpClient()
    return RedisContainerReadiness(services.redis(), client, client)


@dataclass(slots=True)
class _ServedASGI:
    server: uvicorn.Server
    thread: threading.Thread

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.server.config.port}"

    def __enter__(self) -> _ServedASGI:
        self.thread.start()
        _wait_until(lambda: self.server.started, timeout_seconds=2)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, tb
        self.server.should_exit = True
        self.thread.join(timeout=2)


def _serve_asgi_handler(handler_ref: str) -> _ServedASGI:
    port = _available_loopback_port()
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
        thread=threading.Thread(target=server.run, daemon=True),
    )


@dataclass
class _EndpointContainers:
    states: list[SchedulerContainerState] = field(default_factory=list)
    addresses: dict[str, str] = field(default_factory=dict)
    address_maps: dict[str, SchedulerContainerAddressMap] = field(default_factory=dict)

    def list_by_stub(self, stub_id: str) -> list[SchedulerContainerState]:
        return [state for state in self.states if state.stub_id == stub_id]

    def get_container_address(self, container_id: str) -> SchedulerContainerAddress | None:
        address = self.addresses.get(container_id, "")
        if not address:
            return None
        return SchedulerContainerAddress(container_id=container_id, address=address)

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        return self.address_maps.get(
            container_id,
            SchedulerContainerAddressMap(container_id=container_id),
        )


class _CancellingEndpointDispatcher(EndpointInstanceDispatcher):
    def __init__(self, containers: _EndpointContainers, runtime: ApiServices) -> None:
        super().__init__(containers, readiness_probe=_ServingContainers())
        self.runtime = runtime
        self.cancelled_task: Task | None = None

    def select_target(
        self,
        stub_id: str,
        *,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = 1,
    ) -> EndpointDispatchTarget | None:
        _ = container_loads, max_inflight_per_container
        if self.cancelled_task is None:
            self.cancelled_task = _wait_for_endpoint_task(self.runtime)
            self.runtime.tasks.cancel(self.cancelled_task.id)
        return super().select_target(stub_id)


@dataclass
class _WebSocketEchoBackend:
    paths: list[str] = field(default_factory=list)
    task_headers: list[str] = field(default_factory=list)
    _thread: threading.Thread | None = field(default=None, init=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)
    _stop: asyncio.Event | None = field(default=None, init=False)
    _ready: threading.Event = field(default_factory=threading.Event, init=False)
    _port: int = field(default=0, init=False)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self._port}"

    def __enter__(self) -> _WebSocketEchoBackend:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2):
            raise AssertionError("websocket backend did not start")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, tb
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._port = _available_loopback_port()
        async with websockets.asyncio.server.serve(
            self._handler,
            "127.0.0.1",
            self._port,
        ):
            self._ready.set()
            await self._stop.wait()

    async def _handler(self, connection: ServerConnection) -> None:
        request = connection.request
        assert request is not None
        self.paths.append(request.path)
        self.task_headers.append(request.headers.get("x-task-id", ""))
        async for message in connection:
            if isinstance(message, str):
                await connection.send(f"echo:{message}:{request.headers.get('x-task-id', '')}")
            else:
                await connection.send(b"echo:" + bytes(message))
                await connection.close()


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


def _available_loopback_port() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        return server.server_port
    finally:
        server.server_close()


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
        service = EndpointControlService(
            isolated_services,
            dispatcher=EndpointInstanceDispatcher(
                containers, readiness_probe=_readiness(isolated_services)
            ),
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
