from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import cloudpickle
import pytest
from lazycloud.abstractions.serve import write_serve_preview
from lazycloud.abstractions.taskqueue import TaskQueueOperationError
from lazycloud.clients.taskqueue.control import TaskQueueControlClient
from lazycloud.session.task import TaskBatch
from lazycloud.values import cloudpickle_bytes
from shared.deployments import DeploymentKind
from shared.http.compute import ContainerResponse
from shared.http.errors import HttpApiError
from shared.http.gateway import (
    AttachToContainerResponse,
    GetUrlRequest,
    GetUrlResponse,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from shared.http.taskqueues import (
    StartTaskQueueServeResponse,
    TaskQueueInvocationEnvelope,
    TaskQueuePutBody,
    TaskQueuePutResponse,
)
from shared.paths import HOME_ENV
from tests.fakes import FakeDeploymentClient

from lazycloud import App

ReturnT = TypeVar("ReturnT")
T = TypeVar("T")


def _call_runtime(
    function: Callable[..., ReturnT],
    /,
    *args: object,
    **kwargs: object,
) -> ReturnT:
    return function(*args, **kwargs)


def lifecycle_hook_one(context: object) -> None:
    _ = context


def lifecycle_hook_two(context: object) -> None:
    _ = context


LIFECYCLE_HOOK_ONE_REF = f"{lifecycle_hook_one.__module__}:lifecycle_hook_one"
LIFECYCLE_HOOK_TWO_REF = f"{lifecycle_hook_two.__module__}:lifecycle_hook_two"


@dataclass
class RecordingTaskQueueControlChannel:
    payloads: list[dict[str, Any]] = field(default_factory=list)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, str]:
        assert path == "/api/v1/taskqueues/put?workspace=default"
        assert payload is not None
        self.payloads.append(payload)
        return {"task_id": "task-queued"}


@dataclass
class FakeTaskQueueClient:
    payloads: list[tuple[str, bytes]] = field(default_factory=list)
    served: list[tuple[str, int]] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    fail_put: bool = False
    fail_serve: bool = False
    fail_at_index: int | None = None

    def put(self, stub_id: str, payload: bytes) -> TaskQueuePutResponse:
        self.payloads.append((stub_id, payload))
        if self.fail_put or self.fail_at_index == len(self.payloads) - 1:
            raise HttpApiError("queue full", status_code=400)
        if len(self.task_ids) >= len(self.payloads):
            task_id = self.task_ids[len(self.payloads) - 1]
        else:
            task_id = "task-queued"
        return TaskQueuePutResponse(task_id=task_id)

    def start_serve(
        self,
        stub_id: str,
        *,
        timeout: int = 600,
    ) -> StartTaskQueueServeResponse:
        self.served.append((stub_id, timeout))
        if self.fail_serve:
            raise HttpApiError("serve unavailable", status_code=503)
        return StartTaskQueueServeResponse(container_id="ctr-serve")


def test_task_queue_control_client_sends_named_serialized_invocation() -> None:
    channel = RecordingTaskQueueControlChannel()
    invocation = cloudpickle_bytes(TaskQueueInvocationEnvelope(args=("clip.mp4",), kwargs={}))

    response = TaskQueueControlClient(channel).put("stub-queue", invocation)

    assert response.task_id == "task-queued"
    body = TaskQueuePutBody.model_validate(channel.payloads[0])
    assert body.stub_id == "stub-queue"
    assert body.invocation.bytes_value() == invocation
    assert "value_base64" not in channel.payloads[0]


@dataclass
class FakeServeGatewayClient:
    urls: list[GetUrlRequest] = field(default_factory=list)
    attached: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    sync_requests: list[SyncContainerWorkspaceBody] = field(default_factory=list)

    def get_url(self, request: GetUrlRequest) -> GetUrlResponse:
        self.urls.append(request)
        return GetUrlResponse(url=f"{request.external_url}/taskqueue/id/{request.stub_id}")

    def attach_to_container_response(self, container_id: str) -> AttachToContainerResponse:
        self.attached.append(container_id)
        return AttachToContainerResponse(output="serve ready\n", done=True)

    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]:
        _ = poll_interval_seconds
        self.attached.append(container_id)
        yield AttachToContainerResponse(output="serve ready\n", done=False)
        yield AttachToContainerResponse(done=True)

    def stop_container(self, container_id: str) -> ContainerResponse:
        self.stopped.append(container_id)
        return ContainerResponse(
            id=container_id,
            name="preview",
            image="python:3.12",
            command=[],
            workspace_id="default",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def sync_container_workspace(
        self,
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse:
        self.sync_requests.append(body)
        return SyncContainerWorkspaceResponse(path=body.path)


def _bind_internal_state(resource: T, /, **values: object) -> T:
    for name, value in values.items():
        setattr(resource, name, value)
    return resource


def test_task_queue_lifecycle_hooks_flow_to_gateway_stub_request() -> None:
    deployment_client = FakeDeploymentClient(stub_id_from_type=True)

    @App("test").task_queue(
        on_start=lifecycle_hook_one,
        on_running=[lifecycle_hook_one, lifecycle_hook_two],
        on_success=lifecycle_hook_two,
        on_retry=lifecycle_hook_one,
        on_failure=lifecycle_hook_two,
        on_finish=lifecycle_hook_one,
    )
    def transcode(path: str) -> str:
        return path

    _bind_internal_state(transcode, deployment_client=deployment_client)

    spec = transcode.spec()
    transcode.deploy(workspace="media")

    assert spec.lifecycle_hooks.on_start == (LIFECYCLE_HOOK_ONE_REF,)
    assert spec.lifecycle_hooks.on_running == (
        LIFECYCLE_HOOK_ONE_REF,
        LIFECYCLE_HOOK_TWO_REF,
    )
    assert spec.lifecycle_hooks.on_success == (LIFECYCLE_HOOK_TWO_REF,)
    assert spec.lifecycle_hooks.on_retry == (LIFECYCLE_HOOK_ONE_REF,)
    assert spec.lifecycle_hooks.on_failure == (LIFECYCLE_HOOK_TWO_REF,)
    assert spec.lifecycle_hooks.on_finish == (LIFECYCLE_HOOK_ONE_REF,)
    assert deployment_client.stub_requests[0].lifecycle_hooks == spec.lifecycle_hooks


def test_task_queue_serve_emits_prepare_progress_without_preconfigured_terminal(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    queue_client = FakeTaskQueueClient()
    gateway_client = FakeServeGatewayClient()
    deployment_client = FakeDeploymentClient(stub_id="stub-transcribe")

    @App("test").task_queue(name="transcribe")
    def transcribe(path: str) -> str:
        return path

    _bind_internal_state(
        transcribe,
        client=queue_client,
        deployment_client=deployment_client,
        gateway_client=gateway_client,
        resource_client=gateway_client,
        sync_local_dir="",
    )

    served = transcribe.serve(timeout=11)

    output = capsys.readouterr().out
    assert served is not False
    assert served.container_id == "ctr-serve"
    assert "Preparing image for <transcribe>" in output
    assert "Preparing deployment stub" in output
    assert "Deployment stub ready <stub-transcribe>" in output
    assert "=> Invocation details" in output
    assert "=> Serving" in output
    assert queue_client.served == [("stub-transcribe", 11)]
    assert gateway_client.attached == ["ctr-serve"]
    assert deployment_client.stub_requests[0].keep_warm_seconds == 11


def test_task_queue_put_many_resolves_target_once_and_returns_batch() -> None:
    queue_client = FakeTaskQueueClient(task_ids=["task-a", "task-b"])
    deployment_client = FakeDeploymentClient(
        stub_id="stub-transcribe",
        deployment_id="dep-transcribe",
        version=3,
        invoke_url_template="{external_url}/taskqueue/{stub_id}",
    )

    @App("test").task_queue()
    def transcribe(path: str, *, priority: bool = False) -> str:
        return f"{path}:{priority}"

    _bind_internal_state(transcribe, client=queue_client, deployment_client=deployment_client)

    batch = transcribe.target("deployed").put_many(["a.mp4", "b.mp4"], priority=True)

    assert isinstance(batch, TaskBatch)
    assert [handle.task_id for handle in batch] == ["task-a", "task-b"]
    assert deployment_client.stub_requests == []
    assert len(deployment_client.resolve_requests) == 1
    assert [stub_id for stub_id, _ in queue_client.payloads] == [
        "stub-transcribe",
        "stub-transcribe",
    ]
    assert [cloudpickle.loads(payload) for _, payload in queue_client.payloads] == [
        TaskQueueInvocationEnvelope(args=("a.mp4",), kwargs={"priority": True}),
        TaskQueueInvocationEnvelope(args=("b.mp4",), kwargs={"priority": True}),
    ]


def test_task_queue_put_supports_versioned_target_and_handler_kwargs() -> None:
    queue_client = FakeTaskQueueClient(task_ids=["task-a"])
    deployment_client = FakeDeploymentClient(stub_id="stub-transcribe", version=7)

    @App("test").task_queue()
    def transcribe(path: str, *, deployment_version: str, target: str) -> str:
        return f"{path}:{deployment_version}:{target}"

    _bind_internal_state(transcribe, client=queue_client, deployment_client=deployment_client)

    task = transcribe.target("deployed", deployment_version=7).put(
        "a.mp4",
        deployment_version="payload",
        target="payload-target",
    )

    assert task is not False
    assert deployment_client.resolve_requests[0].deployment_version == 7
    assert cloudpickle.loads(queue_client.payloads[0][1]) == TaskQueueInvocationEnvelope(
        args=("a.mp4",),
        kwargs={"deployment_version": "payload", "target": "payload-target"},
    )


def test_task_queue_put_prefers_matching_serve_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    queue_client = FakeTaskQueueClient(task_ids=["task-a"])
    deployment_client = FakeDeploymentClient(stub_id="stub-deployed", fail_resolve=True)

    write_serve_preview(
        kind=DeploymentKind.TaskQueue,
        name="transcribe",
        app="test",
        workspace="default",
        endpoint="http://127.0.0.1:9000",
        stub_id="stub-preview",
        container_id="ctr-preview",
        url="http://127.0.0.1:9000/taskqueue/id/stub-preview",
    )

    @App("test").task_queue(
        name="transcribe",
    )
    def transcribe(path: str) -> str:
        return path

    _bind_internal_state(transcribe, client=queue_client, deployment_client=deployment_client)

    task = transcribe.put("a.mp4")

    assert task is not False
    assert deployment_client.resolve_requests == []
    assert queue_client.payloads[0][0] == "stub-preview"


def test_task_queue_put_many_empty_does_not_prepare() -> None:
    queue_client = FakeTaskQueueClient()
    deployment_client = FakeDeploymentClient(
        stub_id="stub-transcribe",
        deployment_id="dep-transcribe",
        version=3,
        invoke_url_template="{external_url}/taskqueue/{stub_id}",
    )

    @App("test").task_queue()
    def transcribe(path: str) -> str:
        return path

    _bind_internal_state(transcribe, client=queue_client, deployment_client=deployment_client)

    batch = transcribe.put_many([])

    assert isinstance(batch, TaskBatch)
    assert list(batch) == []
    assert deployment_client.stub_requests == []
    assert queue_client.payloads == []


def test_task_queue_put_many_raises_typed_error_on_enqueue_failure() -> None:
    queue_client = FakeTaskQueueClient(
        task_ids=["task-a", "task-b"],
        fail_at_index=1,
    )
    deployment_client = FakeDeploymentClient(
        stub_id="stub-transcribe",
        deployment_id="dep-transcribe",
        version=3,
        invoke_url_template="{external_url}/taskqueue/{stub_id}",
    )

    @App("test").task_queue()
    def transcribe(path: str) -> str:
        return path

    _bind_internal_state(transcribe, client=queue_client, deployment_client=deployment_client)

    with pytest.raises(TaskQueueOperationError, match="item 1"):
        transcribe.target("deployed").put_many(["a.mp4", "b.mp4"])

    assert len(queue_client.payloads) == 2
