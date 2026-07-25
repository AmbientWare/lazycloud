from __future__ import annotations

import io
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from api.server.services import ApiServices
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService, StubKind, StubRecord
from coordination.redis_client import RedisClient
from execution.functions.service import FunctionControlService
from gateway.service import GatewayControlService
from pydantic import JsonValue, TypeAdapter
from runner.function import (
    FunctionRunner,
    FunctionRunnerConfig,
    TaskLogStream,
    decode_function_invocation,
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
    FunctionJsonInvocation,
    FunctionPayloadEncoding,
)
from shared.http.functions import (
    FunctionGetArgsRequest,
    FunctionGetArgsResponse,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionSetResultBody,
)
from shared.http.gateway_tasks import AppendTaskLogRequest, EndTaskRequest, StartTaskRequest
from shared.lifecycle import LifecycleHooks
from shared.tasks import TaskStatus
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_function_runner_streams_plain_user_logs_and_persists_result(
    function_runtime: ApiServices,
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
    responses = _invoke_and_run(function_runtime, handler_ref, 8)

    final = _final_response(responses)
    assert final.exit_code == 0
    assert final.result is not None
    assert final.result.encoding is FunctionPayloadEncoding.Cloudpickle
    assert isinstance(final.result, FunctionCloudpickleResult)
    assert final.result.bytes_value() == cloudpickle_bytes(64)

    output = "".join(item.output for item in responses)
    assert "alpha\n" in output
    assert "beta\n" in output
    assert "gamma\n" in output
    assert "stdout" not in output
    assert "stderr" not in output

    task_id = responses[0].task_id
    logs = function_runtime.tasks.logs(task_id)
    assert [(entry.stream, entry.message) for entry in logs] == [
        ("stdout", "alpha"),
        ("stdout", "beta"),
        ("stdout", "gamma"),
    ]
    assert function_runtime.tasks.get(task_id).status is TaskStatus.Complete


def _services_with_redis(
    isolated_services: ApiServices,
    redis: RedisClient,
    request: pytest.FixtureRequest,
) -> ApiServices:
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=redis,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services


@pytest.fixture
def function_runtime(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    request: pytest.FixtureRequest,
) -> ApiServices:
    return _services_with_redis(isolated_services, real_redis_actors.client(), request)


def test_function_runner_rejects_untyped_invocation_envelopes() -> None:
    with pytest.raises(ValueError, match="invalid function invocation envelope"):
        decode_function_invocation(
            FunctionGetArgsResponse(
                invocation=FunctionCloudpickleInvocation.from_bytes(
                    cloudpickle_bytes(["not", "an", "envelope"])
                )
            )
        )


def test_function_runner_failure_streams_and_persists_traceback(
    function_runtime: ApiServices,
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
    responses = _invoke_and_run(function_runtime, handler_ref)

    final = _final_response(responses)
    assert final.exit_code == 1
    assert final.done

    output = "".join(item.output for item in responses)
    assert "before failure\n" in output
    assert "Traceback" in output
    assert "RuntimeError: boom" in output

    task_id = responses[0].task_id
    logs = function_runtime.tasks.logs(task_id)
    stderr = "\n".join(entry.message for entry in logs if entry.stream == "stderr")
    assert "Traceback" in stderr
    assert "RuntimeError: boom" in stderr
    assert function_runtime.tasks.get(task_id).status is TaskStatus.Failed


def test_task_log_stream_flush_publishes_partial_line_once() -> None:
    runner = _RecordingRunner()
    stream = TaskLogStream(runner, "stdout", io.StringIO())

    assert stream.write("partial") == len("partial")
    assert runner.logs == []

    stream.flush()
    stream.flush_log()

    assert runner.logs == [("stdout", "partial")]


def _invoke_and_run(
    runtime: ApiServices,
    handler_ref: str,
    *args: int,
    raw_payload: bytes | None = None,
    lifecycle_hooks: LifecycleHooks | None = None,
    compute_state: RedisComputeStateRepository | None = None,
) -> list[FunctionInvokeResponse]:
    scheduler = _Scheduler()
    runtime.containers.scheduler = scheduler
    function_service = FunctionControlService(runtime)
    gateway_service = replace(
        runtime.gateway_service,
        compute_state=compute_state or _compute_state(),
    )
    stub = _create_function_stub(runtime, handler_ref)
    if raw_payload is None:
        invocation = FunctionCloudpickleInvocation.from_bytes(
            cloudpickle_bytes({"args": args, "kwargs": {}})
        )
    else:
        decoded = _JSON_OBJECT_ADAPTER.validate_json(raw_payload)
        invocation = FunctionJsonInvocation.model_validate(
            {
                "args": decoded.get("args", []),
                "kwargs": decoded.get("kwargs", {}),
            }
        )
    stream = iter(
        function_service.function_invoke_stream(
            FunctionInvokeBody(
                stub_id=stub.id,
                invocation=invocation,
            ),
            poll_interval_seconds=0.01,
            keepalive_interval_seconds=1.0,
        )
    )
    responses = [next(stream)]
    assert responses[0].task_id
    assert scheduler.requests
    runner = FunctionRunner(
        config=FunctionRunnerConfig(
            task_id=responses[0].task_id,
            stub_id=stub.id,
            handler_ref=handler_ref,
            container_id=scheduler.requests[0].container_id,
            container_hostname="test-host",
            lifecycle_hooks=lifecycle_hooks or LifecycleHooks(),
        ),
        channel=_FunctionRunnerServiceChannel(function_service, gateway_service),
    )

    runner.run()
    responses.extend(stream)
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
        if path == "/api/v1/functions/get-args":
            response = self.function_service.function_get_args(
                FunctionGetArgsRequest.model_validate(payload)
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


def _compute_state() -> RedisComputeStateRepository:
    return RedisComputeStateRepository(RedisClient(FakeRedis(), key_prefix="function-runner"))


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


class _RecordingRunner:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    def append_task_log(self, stream: str, message: str) -> None:
        self.logs.append((stream, message))
