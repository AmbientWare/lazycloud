from __future__ import annotations

import pickle
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Never, Protocol

import pytest
import shared.tasks
from cli.main import build_admin_cli, start
from lazycloud.json_contracts import parse_json_object
from lazycloud.session.task import TaskResult
from pydantic import JsonValue
from shared.app_identity import ADMIN_CLI_NAME
from shared.deployments import StubKind
from shared.function_payloads import FunctionCloudpickleResult, FunctionJsonResult
from shared.http.tasks import TaskResponse, TaskWorkloadReferenceResponse
from shared.tasks import TaskStatus
from typer.testing import CliRunner

full_cli = build_admin_cli()
TASK_CLIENT_TARGET = "lazycloud.cli.resources.task_client"


@dataclass(slots=True)
class _TaskHandle:
    task: shared.tasks.Task

    def result(
        self,
        *,
        wait: bool,
        timeout_seconds: float | None,
    ) -> TaskResult:
        _ = wait, timeout_seconds
        return TaskResult(self.task)


@dataclass(slots=True)
class _TaskClient:
    response: TaskResponse

    def handle(self, task_id: str) -> _TaskHandle:
        assert task_id == self.response.id
        return _TaskHandle(
            shared.tasks.Task(
                id=task_id,
                name=self.response.name,
                status=self.response.status,
                result=self.response.result,
                error=self.response.error,
                exit_code=self.response.exit_code,
            )
        )

    def detail(self, task_id: str) -> TaskResponse:
        assert task_id == self.response.id
        return self.response


class _TaskClientFactory(Protocol):
    def __call__(
        self,
        *,
        workspace: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> _TaskClient: ...


def test_task_result_human_presents_structured_json_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value: dict[str, JsonValue] = {"status": "healthy", "details": ["ready", 2]}
    response = _task_response(
        FunctionJsonResult(value=value).model_dump(mode="json"),
        kind=StubKind.Function,
    )
    monkeypatch.setattr(TASK_CLIENT_TARGET, _task_client_factory(response))

    result = CliRunner().invoke(full_cli, ["task", "result", response.id, "--no-wait"])

    assert result.exit_code == 0
    assert "Status" in result.stdout
    assert "healthy" in result.stdout
    assert "Details" in result.stdout
    assert "ready, 2" in result.stdout
    assert "'status': 'healthy'" not in result.stdout
    assert "value_base64" not in result.stdout


def test_task_result_json_preserves_canonical_json_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value: dict[str, JsonValue] = {"value": 2}
    encoded = FunctionJsonResult(value=value).model_dump(mode="json")
    response = _task_response(encoded)
    monkeypatch.setattr(TASK_CLIENT_TARGET, _task_client_factory(response))

    result = CliRunner().invoke(
        full_cli,
        ["--json", "task", "result", response.id, "--no-wait"],
    )

    assert result.exit_code == 0
    assert parse_json_object(result.stdout)["result"] == encoded


def test_task_result_inspection_never_deserializes_cloudpickle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = FunctionCloudpickleResult.from_bytes(pickle.dumps({"unsafe": "payload"})).model_dump(
        mode="json"
    )
    response = _task_response(encoded)
    monkeypatch.setattr(TASK_CLIENT_TARGET, _task_client_factory(response))

    def fail_deserialize(value: bytes) -> Never:
        pytest.fail(f"Python payload was deserialized: {len(value)} bytes")

    monkeypatch.setattr(
        "lazycloud.function_results.pickle.loads",
        fail_deserialize,
    )

    human = CliRunner().invoke(full_cli, ["task", "result", response.id, "--no-wait"])

    machine = CliRunner().invoke(
        full_cli,
        ["--json", "task", "result", response.id, "--no-wait"],
    )

    assert human.exit_code == 0
    assert "Opaque Python result" in human.stdout
    assert "unsafe" not in human.stdout
    assert machine.exit_code == 0
    assert parse_json_object(machine.stdout)["result"] == encoded


def test_failed_task_result_returns_a_formatted_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = _task_response(
        None,
        status=TaskStatus.Failed,
        error="worker exited before producing a result",
        exit_code=17,
    )
    monkeypatch.setattr(TASK_CLIENT_TARGET, _task_client_factory(response))

    with pytest.raises(SystemExit) as raised:
        start(
            args=["task", "result", response.id, "--no-wait"],
            prog_name=ADMIN_CLI_NAME,
        )

    assert raised.value.code == 17
    captured = capsys.readouterr()
    assert "Task failed" in captured.err
    assert "worker exited before producing a result" in captured.err
    assert captured.out == ""


def _task_client_factory(response: TaskResponse) -> _TaskClientFactory:
    def build_task_client(
        *,
        workspace: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> _TaskClient:
        _ = workspace, timeout_seconds
        return _TaskClient(response)

    return build_task_client


def _task_response(
    result: JsonValue,
    *,
    kind: StubKind = StubKind.Function,
    status: TaskStatus = TaskStatus.Complete,
    error: str | None = None,
    exit_code: int | None = 0,
) -> TaskResponse:
    created_at = datetime(2026, 7, 11, tzinfo=UTC)
    return TaskResponse(
        id="task-result",
        name="function-result",
        status=status,
        result=result,
        error=error,
        exit_code=exit_code,
        created_at=created_at,
        workload=TaskWorkloadReferenceResponse(name="function-result", kind=kind),
    )
