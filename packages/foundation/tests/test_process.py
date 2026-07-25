from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest
from foundation.process import (
    PROCESS_OUTPUT_TRUNCATED_MARKER,
    ProcessOutputChunk,
    ProcessOutputDrainError,
    ProcessOutputStream,
    ProcessTimeoutError,
    run_process,
    start_managed_command,
)


def test_run_process_bounds_each_output_stream() -> None:
    result = run_process(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.write('a' * 100000); sys.stdout.flush(); "
                "sys.stderr.write('b' * 100000); sys.stderr.flush()"
            ),
        ],
        timeout_seconds=5,
        max_output_chars=256,
    )

    assert result.ok
    assert len(result.stdout) == 256
    assert len(result.stderr) == 256
    assert result.stdout.startswith(PROCESS_OUTPUT_TRUNCATED_MARKER)
    assert result.stderr.startswith(PROCESS_OUTPUT_TRUNCATED_MARKER)
    assert result.stdout.endswith("a" * 32)
    assert result.stderr.endswith("b" * 32)


def test_run_process_timeout_escalates_and_reaps_process_group(tmp_path: Path) -> None:
    term_marker = tmp_path / "term"
    pid_path = tmp_path / "pid"
    script = tmp_path / "hang.py"
    script.write_text(
        "\n".join(
            (
                "import os",
                "import signal",
                "import sys",
                "import time",
                f"marker = {str(term_marker)!r}",
                f"pid_path = {str(pid_path)!r}",
                "def on_term(_signal, _frame):",
                "    open(marker, 'w', encoding='utf-8').write('term')",
                "signal.signal(signal.SIGTERM, on_term)",
                "open(pid_path, 'w', encoding='utf-8').write(str(os.getpid()))",
                "sys.stderr.write('x' * 100000)",
                "sys.stderr.flush()",
                "while True:",
                "    time.sleep(1)",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProcessTimeoutError) as raised:
        run_process(
            [sys.executable, str(script)],
            timeout_seconds=0.2,
            termination_timeout_seconds=0.1,
            max_output_chars=256,
        )

    error = raised.value
    assert error.termination_error is None
    assert term_marker.read_text(encoding="utf-8") == "term"
    assert len(error.stderr) == 256
    assert error.stderr.startswith(PROCESS_OUTPUT_TRUNCATED_MARKER)
    pid = int(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"timed-out process {pid} was not reaped")


def test_managed_command_publishes_stdout_and_stderr_before_exit() -> None:
    chunks: list[ProcessOutputChunk] = []
    stdout_seen = threading.Event()
    stderr_seen = threading.Event()

    def capture(chunk: ProcessOutputChunk) -> None:
        chunks.append(chunk)
        if chunk.stream is ProcessOutputStream.Stdout and "stdout-early" in chunk.text:
            stdout_seen.set()
        if chunk.stream is ProcessOutputStream.Stderr and "stderr-early" in chunk.text:
            stderr_seen.set()

    command = start_managed_command(
        [
            sys.executable,
            "-c",
            (
                "import sys, time; "
                "sys.stdout.write('stdout-early'); sys.stdout.flush(); "
                "sys.stderr.write('stderr-early'); sys.stderr.flush(); "
                "time.sleep(1)"
            ),
        ],
        output_sink=capture,
    )

    assert stdout_seen.wait(timeout=0.5)
    assert stderr_seen.wait(timeout=0.5)
    assert command.poll() is None
    result = command.terminate()
    assert result.stdout == "stdout-early"
    assert result.stderr == "stderr-early"


def test_managed_command_keeps_diagnostic_tail_when_output_sink_fails() -> None:
    def fail(_chunk: ProcessOutputChunk) -> None:
        raise RuntimeError("delivery unavailable")

    result = start_managed_command(
        [
            sys.executable,
            "-c",
            "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr)",
        ],
        output_sink=fail,
    ).wait(timeout_seconds=5)

    assert result.ok
    assert result.stdout == "stdout-line\n"
    assert result.stderr == "stderr-line\n"
    assert result.output_sink_failure is not None
    assert result.output_sink_failure.error_type == "RuntimeError"
    assert result.output_sink_failure.message == "delivery unavailable"


def test_managed_command_waits_for_large_output_sink_eof_drain() -> None:
    expected_bytes = 512 * 1024
    captured_bytes = 0
    lock = threading.Lock()

    def capture(chunk: ProcessOutputChunk) -> None:
        nonlocal captured_bytes
        time.sleep(0.005)
        with lock:
            captured_bytes += len(chunk.text.encode("utf-8"))

    result = start_managed_command(
        [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write('x' * {expected_bytes}); sys.stdout.flush()",
        ],
        output_sink=capture,
    ).wait(timeout_seconds=5)

    assert result.ok
    assert captured_bytes == expected_bytes


def test_managed_command_reports_output_pipe_that_does_not_reach_eof() -> None:
    command = start_managed_command(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.2)']); "
                "print('parent-exited')"
            ),
        ],
        output_drain_timeout_seconds=0.02,
    )

    with pytest.raises(ProcessOutputDrainError) as raised:
        command.wait(timeout_seconds=5)

    assert raised.value.pending_streams
    assert raised.value.timeout_seconds == 0.02
