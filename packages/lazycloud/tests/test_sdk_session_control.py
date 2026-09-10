from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial

import pytest
import shared.tasks
from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.session.deployment import DeploymentClient
from lazycloud.session.task import (
    FunctionCall,
    Task,
    TaskClient,
    TaskOperationError,
    TaskSubscription,
)
from pydantic import JsonValue
from shared.deployments import DeploymentKind
from shared.function_payloads import FunctionCloudpickleResult
from shared.http.deployments import DeploymentListResponse, DeploymentResponse
from shared.http.errors import HttpApiError, HttpResponseDecodeError, HttpTransportError
from shared.http.gateway import (
    AttachToContainerResponse,
)
from shared.http.observability import (
    LogRecord,
)
from shared.http.tasks import TaskDetailResponse, TaskPageResponse, TaskResponse, TaskStopResponse
from shared.tasks import TaskStatus
from shared.transport_retry import TransientRetry
from tests.fakes import http_api_error
from tests.url_constants import EXAMPLE_URL


@dataclass
class FakeSessionResources:
    deployment_requests: list[tuple[bool | None, str | None, str | None, bool, int, str | None]] = (
        field(default_factory=list)
    )
    delete_requests: list[str] = field(default_factory=list)
    task_requests: list[
        tuple[tuple[str, ...], TaskStatus | None, str | None, str | None, int, str | None]
    ] = field(default_factory=list)
    stop_requests: list[tuple[str, ...]] = field(default_factory=list)
    deployment_stop_requests: list[str] = field(default_factory=list)
    deployment_start_requests: list[str] = field(default_factory=list)
    deployment_scale_requests: list[tuple[str, int]] = field(default_factory=list)
    task_responses: dict[str, TaskDetailResponse] = field(default_factory=dict)
    fail_deployments: bool = False
    fail_tasks: bool = False

    @staticmethod
    def _deployment() -> DeploymentResponse:
        created_at = datetime(2026, 1, 1, tzinfo=UTC)
        return DeploymentResponse(
            id="dep-1",
            name="worker",
            kind=DeploymentKind.Function,
            stub_id="stub-worker",
            created_at=created_at,
            updated_at=created_at,
        )

    def list_deployments(
        self,
        *,
        active: bool | None = None,
        app_id: str | None = None,
        name: str | None = None,
        latest: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> DeploymentListResponse:
        self.deployment_requests.append((active, app_id, name, latest, limit, cursor))
        if self.fail_deployments:
            raise http_api_error("deployments failed")
        deployments = [self._deployment()]
        if name is not None:
            deployments = [item for item in deployments if item.name == name]
        return DeploymentListResponse(data=deployments)

    def deployment(self, deployment_id: str) -> DeploymentResponse:
        if deployment_id != "dep-1":
            raise http_api_error("deployment not found", status_code=404)
        return self._deployment()

    def delete_deployment(self, deployment_id: str) -> None:
        self.delete_requests.append(deployment_id)

    def stop_deployment(self, deployment_id: str) -> DeploymentResponse:
        self.deployment_stop_requests.append(deployment_id)
        return self._deployment()

    def start_deployment(self, deployment_id: str) -> DeploymentResponse:
        self.deployment_start_requests.append(deployment_id)
        return self._deployment()

    def scale_deployment(self, deployment_id: str, replicas: int) -> DeploymentResponse:
        self.deployment_scale_requests.append((deployment_id, replicas))
        return self._deployment()

    def list_tasks(
        self,
        *,
        stub_ids: tuple[str, ...] = (),
        status: TaskStatus | None = None,
        deployment_id: str | None = None,
        app_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> TaskPageResponse:
        self.task_requests.append((stub_ids, status, deployment_id, app_id, limit, cursor))
        if self.fail_tasks:
            raise http_api_error("tasks failed")
        task = TaskResponse(
            id="task-1",
            name="worker",
            status=TaskStatus.Complete,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        tasks = [task] if status in {None, TaskStatus.Complete} else []
        return TaskPageResponse(data=tasks)

    def task(self, task_id: str) -> TaskDetailResponse:
        if task_id in self.task_responses:
            return self.task_responses[task_id]
        if task_id != "task-1":
            raise http_api_error("task not found", status_code=404)
        return TaskDetailResponse(
            id="task-1",
            name="worker",
            status=TaskStatus.Complete,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def stop_tasks(self, task_ids: tuple[str, ...]) -> TaskStopResponse:
        self.stop_requests.append(task_ids)
        return TaskStopResponse(stopped=list(task_ids))


@pytest.mark.parametrize("resource", ["deployment", "task"])
@pytest.mark.parametrize("failure_kind", ["http-401", "transport"])
def test_resource_get_preserves_non_not_found_failures(
    resource: str,
    failure_kind: str,
) -> None:
    failure = (
        HttpTransportError("GET", EXAMPLE_URL, "offline")
        if failure_kind == "transport"
        else http_api_error(
            "resource read failed", status_code=int(failure_kind.removeprefix("http-"))
        )
    )
    expected_error = HttpTransportError if failure_kind == "transport" else HttpApiError

    class FailingResources(FakeSessionResources):
        def deployment(self, deployment_id: str) -> DeploymentResponse:
            _ = deployment_id
            raise failure

        def task(self, task_id: str) -> TaskDetailResponse:
            _ = task_id
            raise failure

    if resource == "deployment":
        with pytest.raises(expected_error) as raised:
            DeploymentClient(resource_client=FailingResources()).get("dep-1")
        assert raised.value is failure
    else:
        client = TaskClient(client=FailingResources())
        with pytest.raises(expected_error) as raised:
            client.get("task-1")
        assert raised.value is failure
        with pytest.raises(expected_error) as raised_detail:
            client.detail("task-1")
        assert raised_detail.value is failure


def test_task_subscription_rejects_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidSubscriptionChannel:
        def __init__(self, *, endpoint: str, token: str | None, timeout_seconds: float) -> None:
            _ = endpoint, token, timeout_seconds

        def get(self, path: str) -> JsonValue:
            _ = path
            return []

    monkeypatch.setattr("lazycloud.control_clients.HttpChannel", InvalidSubscriptionChannel)

    client = TaskClient(workspace="team", endpoint=EXAMPLE_URL, token="token")
    with pytest.raises(HttpResponseDecodeError, match="invalid response"):
        client.subscribe("task-1")


def test_gateway_control_client_streams_attach_events() -> None:
    class FakeChannel:
        paths: list[str]

        def __init__(self) -> None:
            self.paths = []

        def stream_get(self, path: str) -> Iterator[str]:
            self.paths.append(path)
            yield ": connected\n"
            yield "\n"
            yield "id: ctr-1\n"
            yield "event: output\n"
            yield 'data: {"output": "ready\\n", "done": false}\n'
            yield "\n"
            yield "id: ctr-1\n"
            yield "event: done\n"
            yield 'data: {"output": "", "done": true, "exit_code": 0}\n'
            yield "\n"

        def get(self, path: str) -> object:
            raise AssertionError(path)

        def post(
            self,
            path: str,
            payload: dict[str, object] | None = None,
            *,
            timeout_seconds: float | None = None,
        ) -> object:
            raise AssertionError(path)

    channel = FakeChannel()

    events = list(
        GatewayControlClient(channel).attach_to_container_events(
            "ctr-1",
            poll_interval_seconds=0.5,
        )
    )

    assert events == [
        AttachToContainerResponse(output="ready\n", done=False),
        AttachToContainerResponse(output="", done=True, exit_code=0),
    ]
    assert channel.paths == [
        "/gateway/containers/attach/stream?container_id=ctr-1&poll_interval_seconds=0.5"
        "&workspace=default"
    ]


def test_task_result_validation_does_not_echo_response_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidTaskHttpChannel:
        def __init__(self, *, endpoint: str, token: str | None, timeout_seconds: float) -> None:
            _ = endpoint, token, timeout_seconds

        def get(self, path: str) -> object:
            _ = path
            return {"kwargs": {"api_key": "must-not-appear"}}

    monkeypatch.setattr(
        "lazycloud.clients.resource.control.HttpChannel",
        InvalidTaskHttpChannel,
    )
    client = TaskClient(workspace="team", endpoint=EXAMPLE_URL, token="token")

    with pytest.raises(HttpResponseDecodeError, match="invalid response") as exc:
        client.get_result_task("task-2")

    assert "must-not-appear" not in str(exc.value)


def test_completed_function_call_rejects_malformed_result() -> None:
    call = FunctionCall[int](
        task_id="task-invalid-result",
        client=TaskClient(workspace="team", endpoint=EXAMPLE_URL, token="token"),
        result_payload=FunctionCloudpickleResult.from_bytes(b"not-a-pickle"),
        complete=True,
    )

    with pytest.raises(TaskOperationError, match="task-invalid-result has an invalid result"):
        call.get()


def test_task_async_wait_returns_task_result() -> None:
    client = SequencedTaskClient(
        {
            "task-1": [
                shared.tasks.Task(
                    id="task-1",
                    name="worker",
                    status=TaskStatus.Complete,
                )
            ]
        }
    )

    result = asyncio.run(Task("task-1", client).async_wait())

    assert result.id == "task-1"
    assert result.status is TaskStatus.Complete


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), ConnectionResetError("connection reset by peer")],
)
def test_task_wait_retries_transient_read_failures(
    failure: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lazycloud.session.task.TransientRetry",
        partial(TransientRetry, sleep=lambda _: None),
    )
    client = TransientReadTaskClient(
        [
            shared.tasks.Task(id="task-1", name="worker", status=TaskStatus.Running),
            failure,
            shared.tasks.Task(id="task-1", name="worker", status=TaskStatus.Complete),
        ]
    )

    result = Task("task-1", client).wait(poll_interval_seconds=0)

    assert result.id == "task-1"
    assert result.status is TaskStatus.Complete
    assert client.calls == 3


def test_task_wait_propagates_http_error_responses_immediately() -> None:
    client = TransientReadTaskClient(
        [HttpApiError("task not found", status_code=404)],
    )

    with pytest.raises(HttpApiError):
        Task("task-1", client).wait(poll_interval_seconds=0.01)

    assert client.calls == 1


def test_function_call_gather_preserves_order_and_wait_options() -> None:
    calls = [
        RecordingFunctionCall("first"),
        RecordingFunctionCall("second"),
        RecordingFunctionCall("third"),
    ]

    results = FunctionCall.gather(
        *calls,
        timeout_seconds=12.5,
        poll_interval_seconds=0.25,
    )

    assert results == ["first", "second", "third"]
    assert [call.options for call in calls] == [[(12.5, 0.25)], [(12.5, 0.25)], [(12.5, 0.25)]]


def test_function_call_gather_can_return_exceptions_in_result_slots() -> None:
    failure = TaskOperationError("task failed")
    calls = [
        RecordingFunctionCall("first"),
        RecordingFunctionCall(failure),
        RecordingFunctionCall("third"),
    ]

    with pytest.raises(TaskOperationError, match="task failed"):
        FunctionCall.gather(*calls)

    results = FunctionCall.gather(*calls, return_exceptions=True)

    assert results == ["first", failure, "third"]


class _UnsupportedTaskHandleOperations:
    def get(self, task_id: str) -> TaskDetailResponse:
        raise AssertionError(f"unexpected task view read for {task_id}")

    def logs(
        self,
        task_id: str,
        *,
        workspace: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[LogRecord]:
        _ = workspace, limit, cursor
        raise AssertionError(f"unexpected task log read for {task_id}")

    def subscribe(self, task_id: str) -> TaskSubscription:
        raise AssertionError(f"unexpected task subscription for {task_id}")

    def cancel(self, task_id: str) -> TaskStopResponse:
        raise AssertionError(f"unexpected task cancellation for {task_id}")


@dataclass
class SequencedTaskClient(_UnsupportedTaskHandleOperations):
    tasks: dict[str, list[shared.tasks.Task]]

    def __post_init__(self) -> None:
        self.calls: dict[str, int] = {task_id: 0 for task_id in self.tasks}

    def get_result_task(self, task_id: str) -> shared.tasks.Task:
        sequence = self.tasks[task_id]
        index = min(self.calls[task_id], len(sequence) - 1)
        self.calls[task_id] += 1
        return sequence[index]


@dataclass
class TransientReadTaskClient(_UnsupportedTaskHandleOperations):
    sequence: list[shared.tasks.Task | BaseException]
    calls: int = 0

    def get_result_task(self, task_id: str) -> shared.tasks.Task:
        _ = task_id
        index = min(self.calls, len(self.sequence) - 1)
        self.calls += 1
        value = self.sequence[index]
        if isinstance(value, BaseException):
            raise value
        return value


@dataclass
class RecordingFunctionCall:
    value: object
    options: list[tuple[float | None, float]] = field(default_factory=list)

    def get(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> object:
        self.options.append((timeout_seconds, poll_interval_seconds))
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value
