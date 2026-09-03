from __future__ import annotations

import asyncio
import ssl
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import cloudpickle
import pytest
from lazycloud.abstractions.endpoint import ASGIMessage, ASGIReceive, ASGISend
from lazycloud.abstractions.function import FunctionOperationError
from lazycloud.abstractions.serve import write_serve_preview
from lazycloud.client_contracts import ClientContractError
from lazycloud.json_contracts import validate_json_object
from lazycloud.schema import Integer, Schema
from lazycloud.terminal import Terminal
from lazycloud.values import cloudpickle_bytes
from pydantic import JsonValue
from shared.containers import ContainerStatus
from shared.deployments import DeploymentKind
from shared.env import (
    CONTAINER_ID_ENV,
    IMPORTING_USER_CODE_ENV,
    ROOT_TASK_ID_ENV,
    TASK_ID_ENV,
)
from shared.function_payloads import (
    FunctionCloudpickleInvocation,
    FunctionCloudpickleResult,
    FunctionInvocationPayload,
)
from shared.http.client_manifests import ClientOperationName
from shared.http.compute import (
    ContainerResponse,
    ContainerWithAppPageResponse,
    ContainerWithAppResponse,
)
from shared.http.functions import (
    FUNCTION_CALL_REF_MARKER,
    FunctionCallDependency,
    FunctionInvokeResponse,
)
from shared.http.gateway import (
    AttachToContainerResponse,
    GetUrlRequest,
    GetUrlResponse,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from shared.paths import HOME_ENV
from shared.tasks import TaskPolicy
from tests.fakes import FakeDeploymentClient

from lazycloud import App

T = TypeVar("T")


class BrokenClientContractAnnotation:
    pass


def lifecycle_hook_one(context: object) -> None:
    _ = context


LIFECYCLE_HOOK_ONE_REF = f"{lifecycle_hook_one.__module__}:lifecycle_hook_one"


@dataclass
class FakeFunctionClient:
    invocations: list[tuple[str, bytes, bool]] = field(default_factory=list)
    contexts: list[tuple[str, str, list[FunctionCallDependency]]] = field(default_factory=list)
    fail: bool = False

    def invoke(
        self,
        stub_id: str,
        invocation: FunctionInvocationPayload,
        *,
        detached: bool = False,
        parent_task_id: str = "",
        root_task_id: str = "",
        dependencies: list[FunctionCallDependency] | None = None,
    ) -> Iterator[FunctionInvokeResponse]:
        assert isinstance(invocation, FunctionCloudpickleInvocation)
        args = invocation.bytes_value()
        self.invocations.append((stub_id, args, detached))
        self.contexts.append((parent_task_id, root_task_id, list(dependencies or [])))
        task_id = f"task-{len(self.invocations)}"
        if self.fail:
            yield FunctionInvokeResponse.from_result(
                task_id=task_id,
                output="failed",
                done=True,
                exit_code=1,
            )
            return
        yield FunctionInvokeResponse.from_result(
            task_id=task_id,
            result=FunctionCloudpickleResult.from_bytes(cloudpickle_bytes({"ok": True})),
            done=True,
        )


@dataclass
class RecordingTerminal(Terminal):
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    remote_outputs: list[tuple[str, str]] = field(default_factory=list)

    def write(self, message: str) -> None:
        self.lines.append(message)

    def line(self, message: str = "") -> None:
        self.lines.append(message)

    def warn(self, message: str) -> None:
        self.line(f"WARNING: {message}")

    def error(self, message: str) -> None:
        self.errors.append(message)

    def remote_output(self, message: str, *, stream: str = "stdout") -> None:
        self.remote_outputs.append((stream, message))


@dataclass
class FakeEndpointGatewayClient:
    urls: list[GetUrlRequest] = field(default_factory=list)
    attached: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    sync_requests: list[SyncContainerWorkspaceBody] = field(default_factory=list)

    def get_url(self, request: GetUrlRequest) -> GetUrlResponse:
        self.urls.append(request)
        return GetUrlResponse(url=f"{request.external_url}/endpoint/id/{request.stub_id}")

    def list_containers(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ContainerWithAppPageResponse:
        del limit, cursor
        return ContainerWithAppPageResponse(
            data=[
                ContainerWithAppResponse(
                    container=ContainerResponse(
                        id="ctr-preview",
                        name="preview",
                        image="image-preview",
                        command=[],
                        workspace_id="",
                        status=ContainerStatus.Running,
                        created_at=datetime(2026, 7, 12, tzinfo=UTC),
                    )
                )
            ]
        )

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
            image="image-preview",
            command=[],
            workspace_id="",
            status=ContainerStatus.Stopped,
            created_at=datetime(2026, 7, 12, tzinfo=UTC),
        )

    def sync_container_workspace(
        self,
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse:
        self.sync_requests.append(body)
        return SyncContainerWorkspaceResponse(path=body.path)


@dataclass
class FakeUrlopenCall:
    request: urllib.request.Request
    timeout: float
    context: ssl.SSLContext


class FakeHeaders:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self.values = values

    def keys(self) -> list[str]:
        return list(self.values)

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def get_all(self, key: str) -> list[str]:
        return self.values[key]


class FakeHttpResponse:
    def __init__(
        self,
        *,
        status: int = 202,
        body: bytes = b'{"accepted": true}',
        headers: dict[str, list[str]] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = FakeHeaders(headers or {"content-type": ["application/json"]})

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def read(self) -> bytes:
        return self.body

    def geturl(self) -> str:
        return "http://127.0.0.1:9000/endpoint/id/stub-health"


def _bind_internal_state(resource: T, /, **values: object) -> T:
    for name, value in values.items():
        setattr(resource, name, value)
    return resource


def _json_object(value: Any, name: str) -> dict[str, JsonValue]:
    try:
        return validate_json_object(value)
    except ValueError as error:
        raise AssertionError(f"expected {name} to be an object") from error


def test_function_remote_stops_reading_after_terminal_stream_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(IMPORTING_USER_CODE_ENV, raising=False)
    monkeypatch.delenv(CONTAINER_ID_ENV, raising=False)

    class TerminalStreamClient(FakeFunctionClient):
        def invoke(
            self,
            stub_id: str,
            invocation: FunctionInvocationPayload,
            *,
            detached: bool = False,
            parent_task_id: str = "",
            root_task_id: str = "",
            dependencies: list[FunctionCallDependency] | None = None,
        ) -> Iterator[FunctionInvokeResponse]:
            del stub_id, invocation, detached, parent_task_id, root_task_id, dependencies
            yield FunctionInvokeResponse.from_result(task_id="task-terminal")
            yield FunctionInvokeResponse.from_result(
                task_id="task-terminal",
                result=FunctionCloudpickleResult.from_bytes(cloudpickle_bytes("complete")),
                done=True,
            )
            raise AssertionError("terminal response must end client stream consumption")

    @App("test").function(name="terminal-stream")
    def terminal_stream() -> str:
        return "local"

    _bind_internal_state(
        terminal_stream,
        stub_id="stub-terminal",
        client=TerminalStreamClient(),
    )

    assert terminal_stream.remote() == "complete"


def test_function_remote_submits_child_task_inside_runtime_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(IMPORTING_USER_CODE_ENV, raising=False)
    monkeypatch.setenv(CONTAINER_ID_ENV, "ctr-runtime")
    monkeypatch.setenv(TASK_ID_ENV, "11111111-1111-4111-8111-111111111111")
    monkeypatch.setenv(ROOT_TASK_ID_ENV, "22222222-2222-4222-8222-222222222222")
    client = FakeFunctionClient()
    deployment_client = FakeDeploymentClient(stub_id="stub-runtime")

    @App("test").function(name="adder")
    def add(left: int, right: int = 0) -> int:
        return left + right

    _bind_internal_state(add, client=client, deployment_client=deployment_client)

    assert add.local(2, 3) == 5
    assert add.remote(4, right=6) == {"ok": True}
    assert asyncio.run(add.async_remote(9, right=10)) == {"ok": True}
    spawned = asyncio.run(add.async_spawn(11, right=12))
    assert spawned.task_id == "task-3"
    assert spawned.get() == {"ok": True}
    assert [request.name for request in deployment_client.resolve_requests] == ["adder"]
    assert [stub_id for stub_id, _, _ in client.invocations] == [
        "stub-runtime",
        "stub-runtime",
        "stub-runtime",
    ]
    assert [detached for _, _, detached in client.invocations] == [False, False, True]
    assert client.contexts == [
        (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            [],
        ),
        (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            [],
        ),
        (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            [],
        ),
    ]


def test_function_import_guard_rejects_every_remote_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(IMPORTING_USER_CODE_ENV, "true")
    monkeypatch.delenv(CONTAINER_ID_ENV, raising=False)
    client = FakeFunctionClient()

    @App("test").function(name="guarded")
    def guarded() -> str:
        return "local"

    _bind_internal_state(guarded, stub_id="stub-1", client=client)

    for invoke in (
        guarded,
        guarded.remote,
        guarded.spawn,
        lambda: guarded.spawn_map([]),
        lambda: list(guarded.map([])),
    ):
        with pytest.raises(FunctionOperationError, match="importing user code"):
            invoke()
    with pytest.raises(FunctionOperationError, match="importing user code"):
        asyncio.run(guarded.async_remote())
    with pytest.raises(FunctionOperationError, match="importing user code"):
        asyncio.run(guarded.async_spawn())
    assert client.invocations == []


def test_function_spawn_serializes_call_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(IMPORTING_USER_CODE_ENV, raising=False)
    monkeypatch.delenv(CONTAINER_ID_ENV, raising=False)
    client = FakeFunctionClient()

    @App("test").function(name="upstream")
    def upstream(value: int) -> int:
        return value + 1

    @App("test").function(name="downstream")
    def downstream(value: int, right: int) -> int:
        return value + right

    _bind_internal_state(upstream, stub_id="stub-upstream", client=client)
    _bind_internal_state(downstream, stub_id="stub-downstream", client=client)

    upstream_call = upstream.spawn(1)
    downstream_call = downstream.spawn(upstream_call, right=5)
    batch_downstream_call = downstream.spawn_map([(upstream_call, 8)])[0]

    assert upstream_call.task_id == "task-1"
    assert downstream_call.task_id == "task-2"
    assert batch_downstream_call.task_id == "task-3"
    payload = cloudpickle.loads(client.invocations[1][1])
    assert payload == {
        "args": ({FUNCTION_CALL_REF_MARKER: True, "task_id": "task-1"},),
        "kwargs": {"right": 5},
    }
    assert client.contexts[1][2] == [
        FunctionCallDependency(
            task_id="task-1",
            workspace_id="",
            edge_type="argument",
        )
    ]
    payload = cloudpickle.loads(client.invocations[2][1])
    assert payload == {
        "args": ({FUNCTION_CALL_REF_MARKER: True, "task_id": "task-1"}, 8),
        "kwargs": {},
    }
    assert client.contexts[2][2] == [
        FunctionCallDependency(
            task_id="task-1",
            workspace_id="",
            edge_type="argument",
        )
    ]


def test_a_function_declares_its_own_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schedule is an argument to a function, and it travels with its stub.

    The whole deploy is one call: the stub the deployment is built from carries
    the expression, so the schedule is created where the deployment registers
    rather than by a second request that could fail on its own.
    """

    monkeypatch.delenv(IMPORTING_USER_CODE_ENV, raising=False)
    monkeypatch.delenv(CONTAINER_ID_ENV, raising=False)
    function_client = FakeFunctionClient()
    deployment_client = FakeDeploymentClient(stub_id_from_type=True)

    @App("test").function(cron="*/5 * * * *", name="cron-task")
    def cron_task() -> str:
        return "local"

    _bind_internal_state(
        cron_task,
        client=function_client,
        deployment_client=deployment_client,
    )

    spec = cron_task.spec()
    response = cron_task.deploy(name="nightly-task", workspace="team")

    assert cron_task.local() == "local"
    assert spec.kind is DeploymentKind.Function
    assert spec.name == "cron-task"
    assert spec.cron == "*/5 * * * *"
    # Unset rather than the function default, so the backend can tell a schedule
    # keeping zero from an author who asked for a window.
    assert spec.resources.keep_warm is None
    assert deployment_client.stub_requests[0].stub_type == "function"
    assert deployment_client.stub_requests[0].cron == "*/5 * * * *"
    assert deployment_client.deploy_requests[0].stub_id == "stub-function"
    assert deployment_client.deploy_requests[0].name == "nightly-task"
    assert response.deployment_id == "dep-stub-function"


def test_function_remote_streams_status_logs_and_ignores_keepalives() -> None:
    class StreamingFunctionClient(FakeFunctionClient):
        def invoke(
            self,
            stub_id: str,
            invocation: FunctionInvocationPayload,
            *,
            detached: bool = False,
            parent_task_id: str = "",
            root_task_id: str = "",
            dependencies: list[FunctionCallDependency] | None = None,
        ) -> Iterator[FunctionInvokeResponse]:
            assert isinstance(invocation, FunctionCloudpickleInvocation)
            args = invocation.bytes_value()
            del parent_task_id, root_task_id, dependencies
            self.invocations.append((stub_id, args, detached))
            yield FunctionInvokeResponse.from_result(task_id="task-1")
            yield FunctionInvokeResponse.from_result(task_id="task-1", status="running")
            yield FunctionInvokeResponse.from_result(task_id="task-1")
            yield FunctionInvokeResponse.from_result(
                task_id="task-1",
                output="hello\n",
                stream="stdout",
            )
            yield FunctionInvokeResponse.from_result(
                task_id="task-1",
                output="careful\n",
                stream="stderr",
            )
            yield FunctionInvokeResponse.from_result(
                task_id="task-1",
                result=FunctionCloudpickleResult.from_bytes(cloudpickle_bytes({"ok": True})),
                done=True,
            )

    terminal = RecordingTerminal()
    client = StreamingFunctionClient()

    @App("test").function(name="streamer")
    def streamer() -> dict[str, bool]:
        return {"ok": False}

    _bind_internal_state(
        streamer,
        stub_id="stub-1",
        client=client,
        terminal=terminal,
    )

    assert streamer.remote() == {"ok": True}
    assert [detached for _, _, detached in client.invocations] == [False]
    assert terminal.remote_outputs == [
        ("stdout", "hello\n"),
        ("stderr", "careful\n"),
    ]
    assert any(line.startswith("   Task: task-1 running (") for line in terminal.lines)


def test_function_remote_distinguishes_none_result_from_incomplete_responses() -> None:
    class ResponseFunctionClient(FakeFunctionClient):
        def __init__(self, responses: list[FunctionInvokeResponse]) -> None:
            super().__init__()
            self.responses = responses

        def invoke(
            self,
            stub_id: str,
            invocation: FunctionInvocationPayload,
            *,
            detached: bool = False,
            parent_task_id: str = "",
            root_task_id: str = "",
            dependencies: list[FunctionCallDependency] | None = None,
        ) -> Iterator[FunctionInvokeResponse]:
            assert isinstance(invocation, FunctionCloudpickleInvocation)
            self.invocations.append((stub_id, invocation.bytes_value(), detached))
            self.contexts.append((parent_task_id, root_task_id, list(dependencies or [])))
            return iter(self.responses)

    @App("test").function(name="optional-result")
    def optional_result() -> None:
        return None

    completed = ResponseFunctionClient(
        [
            FunctionInvokeResponse.from_result(
                task_id="task-none",
                result=FunctionCloudpickleResult.from_bytes(cloudpickle_bytes(None)),
                done=True,
            )
        ]
    )
    _bind_internal_state(optional_result, stub_id="stub-1", client=completed)

    assert optional_result.remote() is None
    assert [detached for _, _, detached in completed.invocations] == [False]

    optional_result.client = ResponseFunctionClient(
        [FunctionInvokeResponse.from_result(task_id="task-incomplete")]
    )
    with pytest.raises(FunctionOperationError, match="ended before task task-incomplete completed"):
        optional_result.remote()

    optional_result.client = ResponseFunctionClient([])
    with pytest.raises(FunctionOperationError, match="returned no responses"):
        optional_result.remote()

    optional_result.client = ResponseFunctionClient(
        [FunctionInvokeResponse.from_result(task_id="", output="submission rejected", done=True)]
    )
    with pytest.raises(FunctionOperationError, match="submission rejected"):
        optional_result.remote()

    optional_result.client = ResponseFunctionClient(
        [
            FunctionInvokeResponse.from_result(
                task_id="task-malformed",
                result=FunctionCloudpickleResult.from_bytes(b"not-a-pickle"),
                done=True,
            )
        ]
    )
    with pytest.raises(FunctionOperationError, match="task-malformed has an invalid result"):
        optional_result.remote()


@pytest.mark.parametrize(
    ("resource_kind", "timeout_seconds", "expected_transport_timeout"),
    [
        ("endpoint", 45, 50.0),
        ("endpoint", None, 185.0),
        ("endpoint", 0, 605.0),
        ("asgi", 45, 50.0),
    ],
)
def test_http_request_transport_timeout_follows_dispatch_wait_contract(
    monkeypatch: pytest.MonkeyPatch,
    resource_kind: str,
    timeout_seconds: int | None,
    expected_transport_timeout: float,
) -> None:
    calls: list[FakeUrlopenCall] = []

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> FakeHttpResponse:
        calls.append(FakeUrlopenCall(request=request, timeout=timeout, context=context))
        return FakeHttpResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    deployment_client = FakeDeploymentClient(
        stub_id="stub-http",
        deployment_id="dep-http",
        version=1,
        invoke_url_template="{external_url}/endpoint/id/{stub_id}",
    )
    app = App("test")
    if resource_kind == "endpoint":

        @app.endpoint(name="http", timeout_seconds=timeout_seconds)
        def endpoint_resource() -> dict[str, bool]:
            return {"ok": True}

        _bind_internal_state(endpoint_resource, deployment_client=deployment_client)
        endpoint_resource.request()
    else:

        @app.asgi(name="http", timeout_seconds=timeout_seconds)
        async def asgi_resource(scope: ASGIMessage, receive: ASGIReceive, send: ASGISend) -> None:
            _ = (scope, receive, send)

        _bind_internal_state(asgi_resource, deployment_client=deployment_client)
        asgi_resource.request()

    assert calls[0].timeout == expected_transport_timeout


def test_endpoint_request_prefers_matching_serve_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    calls: list[FakeUrlopenCall] = []

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> FakeHttpResponse:
        calls.append(FakeUrlopenCall(request=request, timeout=timeout, context=context))
        return FakeHttpResponse(body=b"preview")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    deployment_client = FakeDeploymentClient(stub_id="stub-deployed", fail_resolve=True)
    write_serve_preview(
        kind=DeploymentKind.Endpoint,
        name="health",
        app="test",
        workspace="",
        endpoint="http://127.0.0.1:9000",
        stub_id="stub-preview",
        container_id="ctr-preview",
        url="http://127.0.0.1:9000/endpoint/id/stub-preview",
    )

    @App("test").endpoint(name="health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    _bind_internal_state(
        health,
        deployment_client=deployment_client,
        gateway_client=FakeEndpointGatewayClient(),
        resource_client=FakeEndpointGatewayClient(),
    )
    response = health.request()

    assert response.text == "preview"
    assert deployment_client.resolve_requests == []
    assert calls[0].request.full_url == "http://127.0.0.1:9000/endpoint/id/stub-preview"


def test_function_normalizes_mapping_task_policy_at_authoring_boundary() -> None:
    @App("analytics").function(task_policy={"timeout_seconds": 45})
    def normalized_policy() -> str:
        return "ok"

    assert normalized_policy.task_policy == TaskPolicy(timeout_seconds=45)
    assert normalized_policy.spec().resources.timeout_seconds == 45


def test_endpoint_on_start_hook_is_exported_as_an_importable_reference() -> None:
    @App("analytics").endpoint(on_start=lifecycle_hook_one)
    def health() -> dict[str, str]:
        return {"ok": "true"}

    spec = health.spec()

    assert spec.lifecycle_hooks.on_start == (LIFECYCLE_HOOK_ONE_REF,)


def test_explicit_schema_overrides_inferred_client_contract_inputs() -> None:
    @App("analytics").function(
        name="explicit",
        inputs=Schema({"value": Integer()}),
    )
    def explicit(value: str) -> str:
        return value

    contract = explicit.spec().client_contract

    assert contract is not None
    assert contract.operation.name is ClientOperationName.Remote
    assert contract.operation.parameters[0].name == "value"
    assert contract.operation.parameters[0].json_schema["type"] == "integer"


def test_explicit_output_metadata_does_not_override_annotated_client_return() -> None:
    @App("analytics").function(
        name="square",
        inputs=Schema({"value": Integer()}),
        outputs=Schema({"result": Integer()}),
    )
    def square(value: int) -> int:
        return value * value

    spec = square.spec()

    assert spec.client_contract is not None
    assert spec.client_contract.operation.return_schema["type"] == "integer"
    outputs = _json_object(spec.metadata["outputs"], "metadata.outputs")
    fields = _json_object(outputs["fields"], "metadata.outputs.fields")
    result = _json_object(fields["result"], "metadata.outputs.fields.result")
    assert result["type"] == "integer"


def test_client_contract_rejects_unresolved_annotations() -> None:
    with pytest.raises(ClientContractError, match="could not export annotation"):

        @App("analytics").endpoint(name="broken")
        def broken(payload: BrokenClientContractAnnotation) -> None:
            return None
