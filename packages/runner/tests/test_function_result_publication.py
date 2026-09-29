from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Pipe
from pathlib import Path

import pytest
from runner.function import (
    ClaimedTask,
    FunctionInvocation,
    FunctionRunner,
    FunctionRunnerConfig,
    FunctionWorkerEvent,
    FunctionWorkerState,
)
from shared.function_payloads import FunctionJsonResult, FunctionPayloadEncoding
from shared.http.functions import FunctionSetResultBody, FunctionSetResultResponse
from shared.http.gateway_tasks import EndTaskRequest, EndTaskResponse
from shared.lifecycle import LifecycleHooks
from shared.tasks import TaskStatus


@pytest.mark.usefixtures("isolated_imports")
@pytest.mark.parametrize(
    "outcome",
    [
        "lost_response",
        "rejected",
        "cancelled",
        "exhausted",
        "committed",
        "hook_log_failure",
        "settlement_auth",
        "superseded_result",
        "superseded_settlement",
    ],
)
def test_result_delivery_never_reclassifies_successful_user_code(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls_path = tmp_path / "calls"
    hooks_path = tmp_path / "hooks"
    source = tmp_path / "result_handler.py"
    source.write_text(
        "from pathlib import Path\n"
        "def handler():\n"
        f"    with Path({str(calls_path)!r}).open('a') as output: output.write('called\\n')\n"
        "    return 42\n"
        "def success(context):\n"
        f"    Path({str(hooks_path)!r}).write_text(context.status.value)\n"
        + ("    raise RuntimeError('hook failure')\n" if outcome == "hook_log_failure" else "")
    )
    if outcome in {"exhausted", "committed", "superseded_settlement"}:
        monkeypatch.setattr("runner.function._RESULT_PUBLICATION_BUDGET_SECONDS", 0.01)
    attempts: list[FunctionSetResultBody] = []
    settlements: list[EndTaskRequest] = []
    settlement_rejected = threading.Event()
    persisted: FunctionJsonResult | None = None

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal persisted
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/gateway/tasks/end":
                settlement = EndTaskRequest.model_validate_json(body)
                settlements.append(settlement)
                assert settlement.claim_id == "claim"
                assert settlement.container_id == "container"
                assert not settlement.retryable
                if outcome == "settlement_auth":
                    self.reply(403, b'{"detail":"forbidden"}')
                    settlement_rejected.set()
                    return
                self.reply(
                    200,
                    EndTaskResponse(
                        final_status=TaskStatus.Complete if persisted else TaskStatus.Failed,
                        claim_acknowledged=outcome != "superseded_settlement",
                    )
                    .model_dump_json()
                    .encode(),
                )
                return
            if self.path == "/gateway/tasks/log":
                assert outcome == "hook_log_failure"
                self.reply(503, b'{"detail":"logs unavailable"}')
                return
            request = FunctionSetResultBody.model_validate_json(body)
            attempts.append(request)
            assert request.task_id == "task"
            assert request.claim_id == "claim"
            assert request.container_id == "container"
            if outcome in {"rejected", "settlement_auth"}:
                self.reply(422, b'{"detail":"invalid completion"}')
            elif outcome == "exhausted" or (outcome == "lost_response" and len(attempts) == 1):
                self.reply(503, b'{"detail":"database unavailable"}')
            elif outcome == "cancelled":
                self.reply(
                    200,
                    FunctionSetResultResponse(
                        stored=False, status=TaskStatus.Cancelled, claim_acknowledged=False
                    )
                    .model_dump_json()
                    .encode(),
                )
            elif persisted is None:
                assert isinstance(request.result, FunctionJsonResult)
                persisted = request.result
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
            else:
                assert request.result == persisted
                self.reply(
                    200,
                    FunctionSetResultResponse(
                        stored=False, claim_acknowledged=outcome != "superseded_result"
                    )
                    .model_dump_json()
                    .encode(),
                )

        def reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: str) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    receive, send = Pipe(duplex=False)
    runner = FunctionRunner(
        FunctionRunnerConfig(
            stub_id="stub",
            handler_ref=f"{source}:handler",
            endpoint=f"http://127.0.0.1:{server.server_port}",
            container_id="container",
            lifecycle_hooks=LifecycleHooks(on_success=(f"{source}:success",)),
        ),
        worker_channel=send,
    )
    task = ClaimedTask(
        task_id="task",
        root_task_id="task",
        attempt_number=1,
        max_attempts=3,
        claim_id="claim",
        invocation=FunctionInvocation(result_format=FunctionPayloadEncoding.Json),
    )
    interrupted = threading.Event()

    def run_until_closed() -> None:
        try:
            runner.run_task(task)
        except InterruptedError:
            interrupted.set()

    invocation_thread: threading.Thread | None = None
    try:
        if outcome == "settlement_auth":
            invocation_thread = threading.Thread(target=run_until_closed)
            invocation_thread.start()
            assert settlement_rejected.wait(2)
            assert invocation_thread.is_alive()
            assert runner._active_task is task
        else:
            runner.run_task(task)
    finally:
        runner.close()
        if invocation_thread is not None:
            invocation_thread.join(timeout=2)
        server.shutdown()
        server.server_close()
        thread.join()
        send.close()

    states: list[FunctionWorkerState] = []
    try:
        while receive.poll():
            try:
                states.append(FunctionWorkerEvent.model_validate_json(receive.recv_bytes()).state)
            except EOFError:
                break
    finally:
        receive.close()
    if outcome == "settlement_auth":
        assert interrupted.is_set()
        assert states == [FunctionWorkerState.Busy]
    else:
        assert states == [FunctionWorkerState.Busy, FunctionWorkerState.Ready]

    assert calls_path.read_text().splitlines() == ["called"]
    if outcome in {
        "lost_response",
        "committed",
        "hook_log_failure",
        "superseded_result",
        "superseded_settlement",
    }:
        assert persisted == FunctionJsonResult(value=42)
    else:
        assert persisted is None
    if outcome in {"lost_response", "committed", "hook_log_failure"}:
        assert hooks_path.read_text() == TaskStatus.Complete.value
    else:
        assert not hooks_path.exists()
    if outcome == "lost_response":
        assert len(attempts) == 3
        first, last = attempts[0].execution_entry, attempts[-1].execution_entry
        assert first is not None and last is not None
        assert last.elapsed_since_entry_seconds > first.elapsed_since_entry_seconds
    assert bool(settlements) == (
        outcome
        in {
            "rejected",
            "exhausted",
            "committed",
            "settlement_auth",
            "superseded_settlement",
        }
    )


@pytest.mark.usefixtures("isolated_imports")
def test_superseded_failure_does_not_run_terminal_hooks(tmp_path: Path) -> None:
    hook_output = tmp_path / "terminal-hook"
    source = tmp_path / "failure_hooks.py"
    source.write_text(
        "from pathlib import Path\n"
        "def terminal(context):\n"
        f"    Path({str(hook_output)!r}).touch()\n"
    )
    runner = FunctionRunner(
        FunctionRunnerConfig(
            stub_id="stub",
            handler_ref="unused:handler",
            lifecycle_hooks=LifecycleHooks(
                on_failure=(f"{source}:terminal",), on_finish=(f"{source}:terminal",)
            ),
        )
    )
    runner.run_final_failure_hooks(
        ClaimedTask("task", "task", 1, 3, FunctionInvocation(), claim_id="old-claim"),
        RuntimeError("old invocation failed"),
        EndTaskResponse(final_status=TaskStatus.Complete, claim_acknowledged=False),
        duration_seconds=1,
    )
    assert not hook_output.exists()
