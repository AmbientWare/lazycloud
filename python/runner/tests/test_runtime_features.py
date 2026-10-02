"""Lifecycle hooks, task lineage and in-process concurrency over the protocol."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

HOOKED = """
import logging
import sys
import threading
import time

# Configured at import, before any attempt runs.
log = logging.getLogger("hooked")
log.addHandler(logging.StreamHandler(sys.stderr))
log.setLevel(logging.INFO)

from lazycloud._shared.task_context import current_root_task_id, current_task_id

events = []


def record(ctx):
    print(
        f"hook {ctx.hook.value} status={ctx.status.value} attempt={ctx.attempt_number}"
        f"/{ctx.max_attempts} retry={ctx.retry_scheduled} error={ctx.error_type}"
    )


def broken_hook(ctx):
    raise RuntimeError("hook exploded")


def start(ctx):
    print(f"starting {ctx.handler}")


def failing_start(ctx):
    raise RuntimeError("cannot initialize")


def ok():
    print("handler ran")
    return {"task": current_task_id(), "root": current_root_task_id()}


def fail():
    raise ValueError("nope")


barrier = threading.Barrier(2, timeout=10)


def loud(size):
    print("x" * size)
    log.info("logged")
    sys.stdout.buffer.write(b"raw bytes\\n")
    return size


def together(name):
    print(f"{name} waiting")
    barrier.wait()
    time.sleep(0.05)
    print(f"{name} done", file=sys.stderr)
    return name
"""

StartRunner = Callable[[Path], Any]

ALL_HOOKS = {
    name: ["hooked:record"]
    for name in ("on_running", "on_success", "on_error", "on_retry", "on_failure", "on_finish")
}


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "hooked.py").write_text(HOOKED, encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    return tmp_path


def _load(runner: Any, handler: str, **extra: Any) -> dict[str, Any]:
    runner.send({"type": "load", "protocol_version": 1, "handler": handler, **extra})
    header, _ = runner.receive()
    return header


def _invoke(runner: Any, attempt: str, args: list[object], **extra: Any) -> None:
    runner.send(
        {
            "type": "invoke",
            "task_id": f"task-{attempt}",
            "attempt_id": attempt,
            "input_encoding": "json",
            **extra,
        },
        json.dumps({"args": args, "kwargs": {}}).encode(),
    )


def test_hooks_run_around_a_success_in_order(workdir: Path, start_runner: StartRunner) -> None:
    runner = start_runner(workdir)
    hooks = {
        **ALL_HOOKS,
        "on_start": ["hooked:start"],
        "on_success": ["hooked:broken_hook", "hooked:record"],
    }
    assert _load(runner, "hooked:ok", hooks=hooks) == {"type": "loaded"}

    _invoke(runner, "a1", [], root_task_id="root-1", attempt_number=1, max_attempts=2)
    header, payload = runner.receive()

    assert header["type"] == "succeeded"
    assert json.loads(payload) == {"task": "task-a1", "root": "root-1"}
    _, stdout, stderr = runner.close()
    assert stdout.splitlines() == [
        "starting hooked:ok",
        "hook on_running status=running attempt=1/2 retry=False error=",
        "handler ran",
        "hook on_success status=complete attempt=1/2 retry=False error=",
        "hook on_finish status=complete attempt=1/2 retry=False error=",
    ]
    assert "lifecycle hook failed: hooked:broken_hook: RuntimeError: hook exploded" in stderr


def test_a_failure_runs_on_retry_while_attempts_remain(
    workdir: Path, start_runner: StartRunner
) -> None:
    runner = start_runner(workdir)
    _load(runner, "hooked:fail", hooks=ALL_HOOKS)

    _invoke(runner, "first", [], attempt_number=1, max_attempts=2)
    assert runner.receive()[0]["type"] == "failed"
    _invoke(runner, "last", [], attempt_number=2, max_attempts=2)
    assert runner.receive()[0]["type"] == "failed"

    _, stdout, _ = runner.close()
    assert stdout.splitlines() == [
        "hook on_running status=running attempt=1/2 retry=False error=",
        "hook on_error status=failed attempt=1/2 retry=False error=ValueError",
        "hook on_retry status=retry attempt=1/2 retry=True error=ValueError",
        "hook on_finish status=retry attempt=1/2 retry=True error=ValueError",
        "hook on_running status=running attempt=2/2 retry=False error=",
        "hook on_error status=failed attempt=2/2 retry=False error=ValueError",
        "hook on_failure status=failed attempt=2/2 retry=False error=ValueError",
        "hook on_finish status=failed attempt=2/2 retry=False error=ValueError",
    ]


def test_a_failing_on_start_fails_the_load(workdir: Path, start_runner: StartRunner) -> None:
    runner = start_runner(workdir)

    header = _load(runner, "hooked:ok", hooks={"on_start": ["hooked:failing_start"]})

    assert header["type"] == "load_failed"
    assert header["error"]["type"] == "RuntimeError"
    assert header["error"]["message"] == "cannot initialize"
    assert runner.process.wait(timeout=30) == 1


def test_in_process_attempts_run_together_with_their_own_output(
    workdir: Path, start_runner: StartRunner
) -> None:
    runner = start_runner(workdir)
    assert _load(runner, "hooked:together", concurrency=2) == {"type": "loaded"}

    _invoke(runner, "a", ["a"])
    _invoke(runner, "b", ["b"])
    output: dict[str, str] = {"a": "", "b": ""}
    results: dict[str, bytes] = {}
    while len(results) < 2:
        header, payload = runner.receive()
        if header["type"] == "output":
            output[header["attempt_id"]] += f"{header['stream']}:{payload.decode()}"
        else:
            assert header["type"] == "succeeded", header
            results[header["attempt_id"]] = payload

    # Both attempts reached the barrier, so they ran at once.
    assert {k: json.loads(v) for k, v in results.items()} == {"a": "a", "b": "b"}
    assert output == {
        "a": "stdout:a waiting\nstderr:a done\n",
        "b": "stdout:b waiting\nstderr:b done\n",
    }
    assert runner.close()[0] == 0


def test_in_process_output_is_chunked_and_routed(workdir: Path, start_runner: StartRunner) -> None:
    runner = start_runner(workdir)
    assert _load(runner, "hooked:loud", concurrency=2) == {"type": "loaded"}

    _invoke(runner, "a", [2 << 20])
    output: dict[str, bytes] = {"stdout": b"", "stderr": b""}
    while True:
        header, payload = runner.receive()
        if header["type"] != "output":
            break
        assert set(header) == {"type", "attempt_id", "stream"}
        assert header["attempt_id"] == "a"
        assert len(payload) <= 256 << 10
        output[header["stream"]] += payload

    assert header["type"] == "succeeded", header
    assert output["stdout"] == b"x" * (2 << 20) + b"\nraw bytes\n"
    assert output["stderr"] == b"logged\n"
    _, stdout, stderr = runner.close()
    assert (stdout, stderr) == ("", "")
