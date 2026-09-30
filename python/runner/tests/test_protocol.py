from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cloudpickle
import pytest
from runner.invocation import cloudpickle_bytes

HANDLERS = """
import asyncio
import dataclasses
import sys

from pydantic import BaseModel
from shared.task_context import current_task_id


class Point(BaseModel):
    x: int
    y: int


@dataclasses.dataclass
class Total:
    value: int
    point: Point


class Boom(Exception):
    pass


def add(point: Point, scale: int) -> Total:
    print("adding on stdout")
    print("adding on stderr", file=sys.stderr)
    return Total(value=(point.x + point.y) * scale, point=point)


def echo(value):
    return value


def make_set():
    return {1, 2}


def fail():
    raise Boom("exploded")


async def task_id_later():
    await asyncio.sleep(0)
    return current_task_id()


NOT_CALLABLE = 3
"""

SDK_HANDLERS = """
import lazycloud

app = lazycloud.App("demo")


@app.function()
def whoami(count: int) -> dict:
    return {"task": lazycloud.current_task_id(), "count": count}
"""

# The conftest client; importlib mode keeps conftest out of the import path.
StartRunner = Callable[[Path], Any]


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "handlers.py").write_text(HANDLERS, encoding="utf-8")
    (tmp_path / "sdk_handlers.py").write_text(SDK_HANDLERS, encoding="utf-8")
    (tmp_path / "broken.py").write_text("def handler(:\n", encoding="utf-8")
    (tmp_path / "exits.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    # Lets this process unpickle exceptions defined in the user module.
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    return tmp_path


def _json(args: list[object], kwargs: dict[str, object] | None = None) -> bytes:
    return json.dumps({"args": args, "kwargs": kwargs or {}}).encode()


def test_json_invocation_coerces_arguments_and_keeps_output_off_the_socket(
    workdir: Path, start_runner: StartRunner
) -> None:
    runner = start_runner(workdir)
    assert runner.load("handlers:add") == {"type": "loaded"}

    header, payload = runner.invoke(_json([{"x": 1, "y": 2}], {"scale": "3"}))

    assert header == {
        "type": "succeeded",
        "attempt_id": "task-1-attempt",
        "result_encoding": "json",
    }
    assert json.loads(payload) == {"value": 9, "point": {"x": 1, "y": 2}}
    code, stdout, stderr = runner.close()
    assert (code, stdout, stderr) == (0, "adding on stdout\n", "adding on stderr\n")


def test_cloudpickle_invocation_round_trips_python_objects(
    workdir: Path, start_runner: StartRunner
) -> None:
    runner = start_runner(workdir)
    runner.load("handlers:echo")

    value: dict[str, object] = {"set": {1, 2}, "bytes": b"\x00\xff"}
    header, payload = runner.invoke(
        cloudpickle_bytes({"args": [value], "kwargs": {}}), encoding="cloudpickle"
    )

    assert header["type"] == "succeeded"
    assert header["result_encoding"] == "cloudpickle"
    assert cloudpickle.loads(payload) == value


def test_failures_report_the_error_and_keep_serving(
    workdir: Path, start_runner: StartRunner
) -> None:
    runner = start_runner(workdir)
    runner.load("handlers:fail")

    header, payload = runner.invoke(_json([]))
    assert header["type"] == "failed"
    assert header["attempt_id"] == "task-1-attempt"
    assert header["error"]["type"] == "handlers.Boom"
    assert header["error"]["message"] == "exploded"
    assert 'raise Boom("exploded")' in header["error"]["traceback"]
    exception = cloudpickle.loads(payload)
    assert type(exception).__qualname__ == "Boom"
    assert exception.args == ("exploded",)

    header, _ = runner.invoke(_json(["unexpected"]))
    assert header["error"]["type"] == "shared.errors.InvalidInputError"

    header, _ = runner.invoke(b"not json")
    assert header["error"]["type"] == "shared.errors.InvalidInputError"
    assert "invalid json arguments" in header["error"]["message"]

    assert runner.close()[0] == 0


def test_json_results_must_be_json_representable(workdir: Path, start_runner: StartRunner) -> None:
    runner = start_runner(workdir)
    runner.load("handlers:make_set")

    header, payload = runner.invoke(_json([]))

    assert header["type"] == "failed"
    assert header["error"]["type"] == "ValueError"
    assert "cannot be encoded as json" in header["error"]["message"]
    assert isinstance(cloudpickle.loads(payload), ValueError)


def test_async_handler_sees_its_task_id(workdir: Path, start_runner: StartRunner) -> None:
    runner = start_runner(workdir)
    runner.load("handlers:task_id_later")

    first, first_payload = runner.invoke(_json([]), task_id="task-7")
    second, second_payload = runner.invoke(_json([]), task_id="task-8")

    assert (first["type"], json.loads(first_payload)) == ("succeeded", "task-7")
    assert (second["type"], json.loads(second_payload)) == ("succeeded", "task-8")


def test_sdk_function_runs_with_its_task_id(workdir: Path, start_runner: StartRunner) -> None:
    runner = start_runner(workdir)
    assert runner.load("sdk_handlers:whoami") == {"type": "loaded"}

    header, payload = runner.invoke(_json(["4"]), task_id="task-sdk")

    assert header["type"] == "succeeded", header
    assert json.loads(payload) == {"task": "task-sdk", "count": 4}


@pytest.mark.parametrize(
    ("handler", "error_type"),
    [
        ("missing_module:handler", "ModuleNotFoundError"),
        ("broken:handler", "SyntaxError"),
        ("exits:handler", "SystemExit"),
        ("handlers:missing", "AttributeError"),
        ("handlers:NOT_CALLABLE", "TypeError"),
        ("handlers", "ValueError"),
    ],
)
def test_load_failure_is_reported_before_exit(
    workdir: Path, start_runner: StartRunner, handler: str, error_type: str
) -> None:
    runner = start_runner(workdir)

    header = runner.load(handler)

    assert header["type"] == "load_failed"
    assert header["error"]["type"] == error_type
    assert header["error"]["traceback"]
    assert runner.process.wait(timeout=30) == 1


@pytest.mark.parametrize(
    "frame",
    [
        (1 << 20) + 1,
        b'{"type": "invoke", "task_id": "t", "attempt_id": "a", "input_encoding": "json"}',
        b'{"type": "shutdown"}',
    ],
    ids=["oversized-header", "invoke-before-load", "unknown-type"],
)
def test_protocol_violations_exit_non_zero(
    workdir: Path, start_runner: StartRunner, frame: int | bytes
) -> None:
    runner = start_runner(workdir)
    if isinstance(frame, int):
        runner.sock.sendall(frame.to_bytes(4, "big"))
    else:
        runner.send_raw(frame)

    code, stdout, stderr = runner.close()

    assert code == 2
    assert stdout == ""
    assert stderr.startswith("lazycloud runner: ")


def test_socket_closed_before_load_exits_cleanly(workdir: Path, start_runner: StartRunner) -> None:
    assert start_runner(workdir).close() == (0, "", "")
