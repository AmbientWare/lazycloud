from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind, StubRecord
from execution.functions.service import FunctionControlService
from gateway.service import GatewayControlService
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
from shared.http.functions import (
    FunctionClaimRequest,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionSetResultBody,
)
from shared.http.gateway_tasks import AppendTaskLogRequest, EndTaskRequest, StartTaskRequest
from shared.tasks import TaskStatus

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
    assert async_services.tasks.get(task_id).status is TaskStatus.Complete


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


async def _invoke_and_run(
    runtime: ApiServices,
    handler_ref: str,
    *args: int,
) -> list[FunctionInvokeResponse]:
    scheduler = _Scheduler()
    runtime.containers.scheduler = scheduler
    function_service = FunctionControlService(
        runtime,
        async_database=runtime.require_async_io().database,
    )
    gateway_service = runtime.gateway_service
    stub = _create_function_stub(runtime, handler_ref)
    invocation = FunctionCloudpickleInvocation.from_bytes(
        cloudpickle_bytes({"args": args, "kwargs": {}})
    )
    initial = function_service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=invocation,
        )
    )
    responses = [initial]
    assert initial.task_id
    assert scheduler.requests
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
            initial, poll_interval_seconds=0.01, keepalive_interval_seconds=1.0
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
    ) -> JsonValue:
        if payload is None:
            raise AssertionError(f"missing payload for path: {path}")
        if path == "/gateway/tasks/start":
            response = self.gateway_service.start_task(
                StartTaskRequest.model_validate(payload),
                workspace_id=self._task_workspace_id(payload),
            )
            return _JSON_OBJECT_ADAPTER.validate_json(response.model_dump_json())
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
            response = self.function_service.function_claim(
                FunctionClaimRequest.model_validate(payload)
            )
            return _JSON_OBJECT_ADAPTER.validate_json(response.model_dump_json())
        if path == "/api/v1/functions/set-result":
            response = self.function_service.function_set_result(
                FunctionSetResultBody.model_validate(payload)
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
