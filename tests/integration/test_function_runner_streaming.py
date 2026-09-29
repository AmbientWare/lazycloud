from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind, StubRecord
from execution.functions.service import FunctionControlService
from gateway.service import GatewayControlService
from observability.startup_latency import StartupLatencyService
from observability.stream_state import AsyncTaskChangeReader
from pydantic import JsonValue, TypeAdapter
from runner.function import (
    FunctionRunner,
    FunctionRunnerConfig,
)
from runner.invocation import cloudpickle_bytes
from scheduler.containers import (
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
)
from scheduler.state import SchedulerWorkerRequest
from shared.function_payloads import (
    FunctionCloudpickleInvocation,
    FunctionCloudpickleResult,
    FunctionPayloadEncoding,
)
from shared.http.errors import HttpApiError
from shared.http.functions import (
    FunctionClaimRequest,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionRetireRequest,
    FunctionSetResultBody,
)
from shared.http.gateway_tasks import AppendTaskLogRequest, EndTaskRequest
from shared.tasks import TaskStatus
from tests.releases import assign_runtime

pytestmark = pytest.mark.usefixtures("isolated_imports")

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


@pytest.mark.anyio
async def test_function_runner_streams_plain_user_logs_and_persists_result(
    async_services: ApiServices,
    tmp_path: Path,
) -> None:
    handler_ref = _write_handler_module(
        tmp_path,
        """
import sys


def stream_value(value):
    print("alpha", flush=True)
    sys.stdout.write("beta")
    sys.stdout.flush()
    print("gamma", flush=True)
    return value * value
""",
        "stream_value",
    )
    responses = await _invoke_and_run(async_services, handler_ref, 8)

    final = _final_response(responses)
    assert final.exit_code == 0
    assert final.result is not None
    assert final.result.encoding is FunctionPayloadEncoding.Cloudpickle
    assert isinstance(final.result, FunctionCloudpickleResult)
    assert final.result.bytes_value() == cloudpickle_bytes(64)

    streamed_output = [(item.stream, item.output) for item in responses if item.output]
    assert streamed_output == [
        ("stdout", "alpha\n"),
        ("stdout", "beta\n"),
        ("stdout", "gamma\n"),
    ]
    output = "".join(message for _, message in streamed_output)
    assert "stdout" not in output
    assert "stderr" not in output

    task_id = responses[0].task_id
    logs = async_services.tasks.logs(task_id)
    assert [(entry.stream, entry.message) for entry in logs] == [
        ("stdout", "alpha"),
        ("stdout", "beta"),
        ("stdout", "gamma"),
    ]
    task = async_services.tasks.get(task_id)
    assert task.status is TaskStatus.Complete
    assert task.workspace_id is not None
    report = StartupLatencyService(async_services.context.database).read(
        workspace_id=task.workspace_id
    )
    assert report.execution.reported_entries == 1
    assert report.execution.missing_entry_evidence == 0


@pytest.mark.anyio
async def test_function_runner_failure_streams_and_persists_traceback(
    async_services: ApiServices,
    tmp_path: Path,
) -> None:
    handler_ref = _write_handler_module(
        tmp_path,
        """
def fail_value():
    print("before failure", flush=True)
    raise RuntimeError("boom")
""",
        "fail_value",
    )
    responses = await _invoke_and_run(async_services, handler_ref)

    final = _final_response(responses)
    assert final.exit_code == 1
    assert final.done

    output = "".join(item.output for item in responses)
    assert "before failure\n" in output
    assert "Traceback" in output
    assert "RuntimeError: boom" in output

    task_id = responses[0].task_id
    logs = async_services.tasks.logs(task_id)
    stderr = "\n".join(entry.message for entry in logs if entry.stream == "stderr")
    assert "Traceback" in stderr
    assert "RuntimeError: boom" in stderr
    assert async_services.tasks.get(task_id).status is TaskStatus.Failed


@pytest.mark.anyio
async def test_function_log_delivery_failure_reports_lost_lines_and_preserves_result(
    async_services: ApiServices,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler_ref = _write_handler_module(
        tmp_path,
        "def output_value():\n    print('unpersisted output')\n    return 64\n",
        "output_value",
    )

    def unavailable(
        runner: FunctionRunner, task_id: str, stream: str, messages: str | list[str]
    ) -> None:
        raise OSError("log service unavailable")

    monkeypatch.setattr(FunctionRunner, "append_task_logs", unavailable)
    responses = await _invoke_and_run(async_services, handler_ref)
    final = _final_response(responses)
    assert final.exit_code == 0
    assert isinstance(final.result, FunctionCloudpickleResult)
    assert final.result.bytes_value() == cloudpickle_bytes(64)
    assert async_services.tasks.logs(final.task_id) == []
    captured = capsys.readouterr()
    assert "unpersisted output" not in captured.out
    assert (
        f"task {final.task_id}: log delivery failed for 1 log line: "
        "OSError: log service unavailable" in captured.err
    )


@pytest.mark.anyio
@pytest.mark.parametrize("committed", [False, True])
async def test_unbounded_function_settles_unconfirmed_publication_without_retry(
    async_services: ApiServices,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    calls = tmp_path / "calls"
    handler_ref = _write_handler_module(
        tmp_path,
        "from pathlib import Path\n"
        "def result_value():\n"
        f"    with Path({str(calls)!r}).open('a') as output: output.write('called\\n')\n"
        "    return 42\n",
        "result_value",
    )
    stub = ControlPlaneService(async_services.context).create_stub(
        "unbounded-function",
        kind=StubKind.Function,
        handler=handler_ref,
        config={"runtime": {"image_id": "image-fn", "timeout_seconds": 0, "retries": 2}},
    )
    post = _FunctionRunnerServiceChannel.post

    def fail_publication(
        channel: _FunctionRunnerServiceChannel,
        path: str,
        payload: dict[str, JsonValue] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> JsonValue:
        if path == "/api/v1/functions/set-result":
            if committed:
                post(channel, path, payload, timeout_seconds=timeout_seconds)
            raise HttpApiError("completion response rejected", status_code=422)
        return post(channel, path, payload, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(_FunctionRunnerServiceChannel, "post", fail_publication)
    responses = await _invoke_and_run(async_services, handler_ref, stub=stub)
    task = async_services.tasks.get(_final_response(responses).task_id)
    assert task.status is (TaskStatus.Complete if committed else TaskStatus.Failed)
    assert task.attempt_number == 1
    assert task.next_retry_at is None
    assert task.max_attempts == 3
    assert calls.read_text().splitlines() == ["called"]
    if committed:
        assert isinstance(task.function_result, FunctionCloudpickleResult)
        assert task.function_result.bytes_value() == cloudpickle_bytes(42)


async def _invoke_and_run(
    runtime: ApiServices,
    handler_ref: str,
    *args: int,
    stub: StubRecord | None = None,
) -> list[FunctionInvokeResponse]:
    scheduler = _Scheduler()
    runtime.containers.scheduler = scheduler
    function_service = FunctionControlService(
        runtime,
        async_database=runtime.require_async_io().database,
        task_changes=AsyncTaskChangeReader(runtime.require_async_io().realtime),
    )
    gateway_service = runtime.gateway_service
    stub = stub or _create_function_stub(runtime, handler_ref)
    invocation = FunctionCloudpickleInvocation.from_bytes(
        cloudpickle_bytes({"args": args, "kwargs": {}})
    )
    initial = function_service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=invocation,
        ),
        stub=stub,
    )
    responses = [initial]
    assert initial.task_id
    assert scheduler.requests
    assign_runtime(
        runtime.containers, runtime.scheduler_workers, scheduler.requests[0].container_id
    )
    runner = FunctionRunner(
        config=FunctionRunnerConfig(
            stub_id=stub.id,
            handler_ref=handler_ref,
            container_id=scheduler.requests[0].container_id,
            container_hostname="test-host",
            keep_warm_seconds=0,
        ),
        channel=_FunctionRunnerServiceChannel(function_service, gateway_service),
    )

    runner.run()
    streamed = [
        response
        async for response in function_service.function_invoke_stream(
            initial, keepalive_interval_seconds=1.0
        )
    ]
    assert streamed[0] == initial
    responses.extend(streamed[1:])
    return responses


def _create_function_stub(services: ApiServices, handler_ref: str) -> StubRecord:
    return ControlPlaneService(services.context).create_stub(
        "streaming-function",
        kind=StubKind.Function,
        handler=handler_ref,
        config={"runtime": {"image_id": "image-fn"}},
    )


def _write_handler_module(tmp_path: Path, source: str, function_name: str) -> str:
    path = tmp_path / f"{function_name}_handler.py"
    path.write_text(source, encoding="utf-8")
    return f"{path}:{function_name}"


def _final_response(responses: list[FunctionInvokeResponse]) -> FunctionInvokeResponse:
    terminal = [item for item in responses if item.done]
    assert terminal
    return terminal[-1]


class _FunctionRunnerServiceChannel:
    def __init__(
        self,
        function_service: FunctionControlService,
        gateway_service: GatewayControlService,
    ) -> None:
        self.function_service = function_service
        self.gateway_service = gateway_service

    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> JsonValue:
        if payload is None:
            raise AssertionError(f"missing payload for path: {path}")
        if path == "/gateway/tasks/log":
            response = self.gateway_service.append_task_log(
                AppendTaskLogRequest.model_validate(payload),
                workspace_id=self._task_workspace_id(payload),
            )
            return _JSON_OBJECT_ADAPTER.validate_json(response.model_dump_json())
        if path == "/gateway/tasks/end":
            response = self.gateway_service.end_task(
                EndTaskRequest.model_validate(payload),
                workspace_id=self._task_workspace_id(payload),
            )
            return _JSON_OBJECT_ADAPTER.validate_json(response.model_dump_json())
        if path == "/api/v1/functions/claim":
            claim = FunctionClaimRequest.model_validate(payload)
            response = self.function_service.function_claim(
                claim,
                workspace_id=self.function_service.control_plane.get_stub(
                    claim.stub_id
                ).workspace_id,
            )
            return _JSON_OBJECT_ADAPTER.validate_json(response.model_dump_json())
        if path == "/gateway/functions/retire":
            retirement = FunctionRetireRequest.model_validate(payload)
            workspace_id = self.function_service.control_plane.get_stub(
                retirement.stub_id
            ).workspace_id
            response = self.function_service.function_retire(retirement, workspace_id=workspace_id)
            return _JSON_OBJECT_ADAPTER.validate_json(response.model_dump_json())
        if path == "/api/v1/functions/set-result":
            response = self.function_service.function_set_result(
                FunctionSetResultBody.model_validate(payload),
                workspace_id=self._task_workspace_id(payload),
            )
            return _JSON_OBJECT_ADAPTER.validate_json(response.model_dump_json())
        raise AssertionError(f"unexpected path: {path}")

    def _task_workspace_id(self, payload: dict[str, JsonValue]) -> str:
        task_id = payload.get("task_id")
        assert isinstance(task_id, str)
        task = self.gateway_service.services.tasks.get(task_id)
        assert task is not None
        assert task.workspace_id is not None
        return task.workspace_id


class _Scheduler:
    def __init__(self) -> None:
        self.requests: list[SchedulerWorkerRequest] = []

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
            reason="queued",
        )
