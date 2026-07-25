from __future__ import annotations

import os
import signal
import subprocess
import threading
from codecs import getincrementaldecoder
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import BinaryIO

from shared.enums import StringEnum

DEFAULT_PROCESS_TERMINATION_TIMEOUT_SECONDS = 2.0
DEFAULT_PROCESS_OUTPUT_MAX_CHARS = 64 * 1024
DEFAULT_PROCESS_OUTPUT_DRAIN_TIMEOUT_SECONDS = 5.0
PROCESS_OUTPUT_TRUNCATED_MARKER = "[earlier process output truncated]\n"
PROCESS_OUTPUT_READ_CHARS = 8192


@dataclass(frozen=True)
class ProcessResult:
    args: list[str]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class ManagedCommandState(StringEnum):
    Running = "running"
    Exited = "exited"
    Terminated = "terminated"


class ProcessOutputStream(StringEnum):
    Stdout = "stdout"
    Stderr = "stderr"


@dataclass(frozen=True, slots=True)
class ProcessOutputChunk:
    stream: ProcessOutputStream
    text: str


@dataclass(frozen=True, slots=True)
class ProcessOutputSinkFailure:
    error_type: str
    message: str


ProcessOutputSink = Callable[[ProcessOutputChunk], None]


class ManagedCommandStillRunning(TimeoutError):
    pass


class ProcessTimeoutError(TimeoutError):
    def __init__(
        self,
        *,
        args: list[str],
        pid: int,
        timeout_seconds: float,
        stdout: str,
        stderr: str,
        termination_error: ManagedCommandStillRunning | OSError | None = None,
    ) -> None:
        self.args_list = list(args)
        self.pid = pid
        self.timeout_seconds = timeout_seconds
        self.stdout = stdout
        self.stderr = stderr
        self.termination_error = termination_error
        suffix = " and could not be reaped" if termination_error is not None else ""
        super().__init__(f"process {pid} exceeded {timeout_seconds:g} seconds{suffix}")


class ProcessOutputDrainError(RuntimeError):
    def __init__(
        self,
        *,
        args: list[str],
        pid: int,
        timeout_seconds: float,
        pending_streams: tuple[ProcessOutputStream, ...],
    ) -> None:
        self.args_list = list(args)
        self.pid = pid
        self.timeout_seconds = timeout_seconds
        self.pending_streams = pending_streams
        streams = ", ".join(stream.value for stream in pending_streams)
        super().__init__(
            f"process {pid} output did not reach EOF within {timeout_seconds:g} seconds: {streams}"
        )


@dataclass(frozen=True)
class ManagedCommandResult:
    args: list[str]
    pid: int
    exit_code: int
    output: str
    state: ManagedCommandState
    stdout: str = ""
    stderr: str = ""
    output_sink_failure: ProcessOutputSinkFailure | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class ManagedCommand:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        args: list[str],
        *,
        max_output_chars: int,
        output_drain_timeout_seconds: float,
        process_group_id: int | None,
        output_sink: ProcessOutputSink | None = None,
    ) -> None:
        self._process = process
        self._args = args
        self._stdout = _BoundedText(max_output_chars)
        self._stderr = _BoundedText(max_output_chars)
        self._output = _BoundedText(max_output_chars)
        self._output_lock = threading.Lock()
        self._output_sink = output_sink
        self._output_sink_failure: ProcessOutputSinkFailure | None = None
        self._output_drain_timeout_seconds = output_drain_timeout_seconds
        self._process_group_id = process_group_id
        self._output_drain_aborted = threading.Event()
        self._state = ManagedCommandState.Running
        self._readers = [
            (
                ProcessOutputStream.Stdout,
                threading.Thread(
                    target=self._capture_output,
                    args=(process.stdout, ProcessOutputStream.Stdout, self._stdout),
                    daemon=True,
                ),
            ),
            (
                ProcessOutputStream.Stderr,
                threading.Thread(
                    target=self._capture_output,
                    args=(process.stderr, ProcessOutputStream.Stderr, self._stderr),
                    daemon=True,
                ),
            ),
        ]
        for _, reader in self._readers:
            reader.start()

    @classmethod
    def start(
        cls,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        max_output_chars: int = DEFAULT_PROCESS_OUTPUT_MAX_CHARS,
        output_drain_timeout_seconds: float = DEFAULT_PROCESS_OUTPUT_DRAIN_TIMEOUT_SECONDS,
        output_sink: ProcessOutputSink | None = None,
    ) -> ManagedCommand:
        if max_output_chars <= 0:
            raise ValueError("managed command output limit must be positive")
        if output_drain_timeout_seconds <= 0:
            raise ValueError("managed command output drain timeout must be positive")
        start_session = hasattr(os, "setsid")
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=start_session,
        )
        return cls(
            process,
            args,
            max_output_chars=max_output_chars,
            output_drain_timeout_seconds=output_drain_timeout_seconds,
            process_group_id=process.pid if start_session else None,
            output_sink=output_sink,
        )

    @property
    def pid(self) -> int:
        return self._process.pid

    def output(self) -> str:
        with self._output_lock:
            return self._output.value()

    def stdout(self) -> str:
        with self._output_lock:
            return self._stdout.value()

    def stderr(self) -> str:
        with self._output_lock:
            return self._stderr.value()

    def output_sink_failure(self) -> ProcessOutputSinkFailure | None:
        with self._output_lock:
            return self._output_sink_failure

    def poll(self) -> ManagedCommandResult | None:
        exit_code = self._process.poll()
        if exit_code is None:
            return None
        self._join_readers()
        state = (
            self._state
            if self._state != ManagedCommandState.Running
            else ManagedCommandState.Exited
        )
        return ManagedCommandResult(
            args=self._args,
            pid=self.pid,
            exit_code=exit_code,
            output=self.output(),
            state=state,
            stdout=self.stdout(),
            stderr=self.stderr(),
            output_sink_failure=self.output_sink_failure(),
        )

    def wait(self, *, timeout_seconds: float | None = None) -> ManagedCommandResult:
        try:
            exit_code = self._process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise ManagedCommandStillRunning("managed command is still running") from exc
        self._join_readers()
        state = (
            self._state
            if self._state != ManagedCommandState.Running
            else ManagedCommandState.Exited
        )
        return ManagedCommandResult(
            args=self._args,
            pid=self.pid,
            exit_code=exit_code,
            output=self.output(),
            state=state,
            stdout=self.stdout(),
            stderr=self.stderr(),
            output_sink_failure=self.output_sink_failure(),
        )

    def terminate(self, *, timeout_seconds: float = 5) -> ManagedCommandResult:
        if self.poll() is not None:
            return self.wait(timeout_seconds=0)
        self._state = ManagedCommandState.Terminated
        self._signal(signal.SIGTERM)
        try:
            return self.wait(timeout_seconds=timeout_seconds)
        except ManagedCommandStillRunning:
            self._signal(signal.SIGKILL)
            return self.wait(timeout_seconds=timeout_seconds)

    def _capture_output(
        self,
        stream: BinaryIO | None,
        stream_name: ProcessOutputStream,
        destination: _BoundedText,
    ) -> None:
        if stream is None:
            return
        decoder = getincrementaldecoder("utf-8")(errors="replace")
        while True:
            try:
                data = os.read(stream.fileno(), PROCESS_OUTPUT_READ_CHARS)
            except OSError:
                if self._output_drain_aborted.is_set():
                    return
                raise
            if not data:
                break
            chunk = decoder.decode(data)
            if not chunk:
                continue
            output = ProcessOutputChunk(stream=stream_name, text=chunk)
            with self._output_lock:
                destination.append(chunk)
                self._output.append(chunk)
            self._publish_output(output)
        final_chunk = decoder.decode(b"", final=True)
        if final_chunk:
            output = ProcessOutputChunk(stream=stream_name, text=final_chunk)
            with self._output_lock:
                destination.append(final_chunk)
                self._output.append(final_chunk)
            self._publish_output(output)

    def _publish_output(self, chunk: ProcessOutputChunk) -> None:
        if self._output_sink is None:
            return
        try:
            self._output_sink(chunk)
        except Exception as exc:  # Callback isolation preserves process diagnostics.
            with self._output_lock:
                if self._output_sink_failure is None:
                    self._output_sink_failure = ProcessOutputSinkFailure(
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )

    def _join_readers(self) -> None:
        deadline = monotonic() + self._output_drain_timeout_seconds
        for _, reader in self._readers:
            reader.join(timeout=max(deadline - monotonic(), 0))
        pending_streams = tuple(stream for stream, reader in self._readers if reader.is_alive())
        if not pending_streams:
            return
        self._output_drain_aborted.set()
        if self._process_group_id is not None:
            with suppress(ProcessLookupError):
                os.killpg(self._process_group_id, signal.SIGKILL)
            for _, reader in self._readers:
                reader.join(timeout=0.2)
        raise ProcessOutputDrainError(
            args=self._args,
            pid=self.pid,
            timeout_seconds=self._output_drain_timeout_seconds,
            pending_streams=pending_streams,
        )

    def _signal(self, sig: signal.Signals) -> None:
        if self._process.poll() is not None:
            return
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(self.pid), sig)
                return
            except ProcessLookupError:
                return
        self._process.send_signal(sig)


class _BoundedText:
    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self._tail = ""
        self._truncated = False

    def append(self, chunk: str) -> None:
        combined = self._tail + chunk
        if len(combined) <= self._max_chars:
            self._tail = combined
            return
        self._truncated = True
        marker_size = min(len(PROCESS_OUTPUT_TRUNCATED_MARKER), self._max_chars)
        tail_size = self._max_chars - marker_size
        self._tail = combined[-tail_size:] if tail_size > 0 else ""

    def value(self) -> str:
        if not self._truncated:
            return self._tail
        marker = PROCESS_OUTPUT_TRUNCATED_MARKER[: self._max_chars]
        return marker + self._tail


def run_process(
    args: list[str],
    *,
    timeout_seconds: float | None = None,
    termination_timeout_seconds: float = DEFAULT_PROCESS_TERMINATION_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_PROCESS_OUTPUT_MAX_CHARS,
    output_drain_timeout_seconds: float = DEFAULT_PROCESS_OUTPUT_DRAIN_TIMEOUT_SECONDS,
    output_sink: ProcessOutputSink | None = None,
) -> ProcessResult:
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("process timeout must be positive")
    if termination_timeout_seconds <= 0:
        raise ValueError("process termination timeout must be positive")
    command = start_managed_command(
        args,
        max_output_chars=max_output_chars,
        output_drain_timeout_seconds=output_drain_timeout_seconds,
        output_sink=output_sink,
    )
    try:
        completed = command.wait(timeout_seconds=timeout_seconds)
    except ManagedCommandStillRunning as wait_error:
        termination_error: ManagedCommandStillRunning | OSError | None = None
        try:
            command.terminate(timeout_seconds=termination_timeout_seconds)
        except (ManagedCommandStillRunning, OSError) as exc:
            termination_error = exc
        raise ProcessTimeoutError(
            args=args,
            pid=command.pid,
            timeout_seconds=timeout_seconds or 0,
            stdout=command.stdout(),
            stderr=command.stderr(),
            termination_error=termination_error,
        ) from wait_error
    return ProcessResult(
        args=args,
        exit_code=completed.exit_code,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def start_managed_command(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    max_output_chars: int = DEFAULT_PROCESS_OUTPUT_MAX_CHARS,
    output_drain_timeout_seconds: float = DEFAULT_PROCESS_OUTPUT_DRAIN_TIMEOUT_SECONDS,
    output_sink: ProcessOutputSink | None = None,
) -> ManagedCommand:
    return ManagedCommand.start(
        args,
        env=env,
        cwd=cwd,
        max_output_chars=max_output_chars,
        output_drain_timeout_seconds=output_drain_timeout_seconds,
        output_sink=output_sink,
    )


def run_command_with_timeout(timeout_seconds: float, args: list[str]) -> ProcessResult:
    return run_process(args, timeout_seconds=timeout_seconds)
