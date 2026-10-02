"""Serve the local runner protocol on the socket the supervisor passes in.

Frames are a 4-byte big-endian header length, a UTF-8 JSON header, a 4-byte
big-endian payload length and the payload bytes. The schemas live in
contracts/runner.yaml. Stdout and stderr belong to the user's task output, so
protocol errors are reported on stderr only when the runner is about to exit.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pickle
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from shared.errors import InvalidInputError
from shared.lifecycle import LifecycleHookName, LifecycleHooks, LifecycleTaskContext
from shared.serialization import to_json_value
from shared.task_context import task_context
from shared.tasks import TaskStatus

from runner import routed_output
from runner.handler_loading import load_handler
from runner.hooks import hooks_from_frame, run_startup_hooks, run_task_hooks, startup_context
from runner.invocation import cloudpickle_bytes, invoke_handler
from runner.protocol_models import (
    Arguments,
    Dependency,
    Encoding,
    Failed,
    Invoke,
    Load,
    Loaded,
    LoadFailed,
    Output,
    RunnerError,
    Stream,
    Succeeded,
)

RUNNER_FD_ENV = "LAZYCLOUD_RUNNER_FD"
MAX_HEADER_BYTES = 1 << 20
MAX_PAYLOAD_BYTES = 64 << 20
PROTOCOL_ERROR_EXIT = 2
LOAD_FAILED_EXIT = 1

_INBOUND: TypeAdapter[Load | Dependency | Invoke] = TypeAdapter(
    Annotated[Load | Dependency | Invoke, Field(discriminator="type")]
)
_FUNCTION_CALL = "function_call"

# Upstream results by task id, for the invoke that follows them.
Dependencies = dict[str, tuple[Encoding, bytearray]]


class ProtocolError(Exception):
    """The supervisor sent a frame the protocol does not allow."""


class Connection:
    """Frame reads and writes on the supervisor socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        # Attempt threads send concurrently; a frame is never interleaved.
        self._send_lock = threading.Lock()

    def receive(self) -> tuple[Load | Dependency | Invoke, bytearray] | None:
        """Read one frame, or None when the supervisor closed between frames."""

        prefix = self._read_prefix()
        if prefix is None:
            return None
        header_length = int.from_bytes(prefix, "big")
        if header_length > MAX_HEADER_BYTES:
            raise ProtocolError(f"header of {header_length} bytes exceeds {MAX_HEADER_BYTES}")
        header = self._read(header_length)
        payload_length = int.from_bytes(self._read(4), "big")
        if payload_length > MAX_PAYLOAD_BYTES:
            raise ProtocolError(f"payload of {payload_length} bytes exceeds {MAX_PAYLOAD_BYTES}")
        payload = self._read(payload_length)
        try:
            message = _INBOUND.validate_json(header)
        except ValidationError as exc:
            raise ProtocolError(f"invalid frame header: {exc}") from None
        return message, payload

    def send(self, header: BaseModel, payload: bytes = b"") -> None:
        encoded = header.model_dump_json(exclude_none=True).encode()
        with self._send_lock:
            self._sock.sendall(
                len(encoded).to_bytes(4, "big") + encoded + len(payload).to_bytes(4, "big")
            )
            if payload:
                self._sock.sendall(payload)

    def wait_closed(self) -> None:
        """Block until the supervisor closes the socket; HTTP workers get no frames."""

        while True:
            try:
                if not self._sock.recv(4096):
                    return
            except OSError:
                return

    def _read_prefix(self) -> bytearray | None:
        """The next frame's header length bytes, or None on a clean close."""

        first = self._sock.recv(1)
        if not first:
            return None
        return bytearray(first) + self._read(3)

    def _read(self, size: int) -> bytearray:
        buffer = bytearray(size)
        view = memoryview(buffer)
        filled = 0
        while filled < size:
            count = self._sock.recv_into(view[filled:])
            if count == 0:
                raise ProtocolError("supervisor closed the socket inside a frame")
            filled += count
        return buffer


def serve(connection: Connection) -> int:
    """Load the handler, then run invocations or serve HTTP until the supervisor closes."""

    frame = connection.receive()
    if frame is None:
        return 0
    load, _ = frame
    if not isinstance(load, Load):
        raise ProtocolError("expected load as the first frame")
    hooks = hooks_from_frame(load.hooks)
    if load.concurrency > 1 or load.http is not None:
        # Before user code loads, so module-level loggers write through it.
        routed_output.install()
    try:
        handler = load_handler(load.handler)
        run_startup_hooks(hooks, load.handler)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        _flush_output()
        connection.send(LoadFailed(type="load_failed", error=_runner_error(exc)))
        return LOAD_FAILED_EXIT
    _flush_output()
    if load.http is not None:
        # uvicorn and asyncio load only for HTTP workers; a function runner
        # starts without them.
        from runner.http import serve_http

        return serve_http(connection, handler, load.http)
    connection.send(Loaded(type="loaded"))
    attempt = _Attempts(handler, hooks, load.handler)
    if load.concurrency > 1:
        return _serve_threads(connection, attempt, load.concurrency)
    dependencies: Dependencies = {}
    while (frame := connection.receive()) is not None:
        invoke, payload = frame
        if isinstance(invoke, Dependency):
            dependencies[invoke.task_id] = (invoke.encoding, payload)
            continue
        if not isinstance(invoke, Invoke):
            raise ProtocolError("load after the handler loaded")
        reply, result = attempt.run(invoke, payload, dependencies)
        dependencies = {}
        _flush_output()
        connection.send(reply, result)
    return 0


def _serve_threads(connection: Connection, attempt: _Attempts, concurrency: int) -> int:
    """Run up to `concurrency` attempts at once, one thread each, sending each
    attempt's output as frames. Closing the socket ends the process; the
    supervisor closes it only once no attempt runs."""

    def send_output(attempt_id: str, stream: Stream, data: str) -> None:
        for chunk in routed_output.utf8_chunks(data):
            connection.send(Output(type="output", attempt_id=attempt_id, stream=stream), chunk)

    def run(invoke: Invoke, payload: bytearray, dependencies: Dependencies) -> None:
        with routed_output.attempt_output(invoke.attempt_id, send_output):
            reply, result = attempt.run(invoke, payload, dependencies)
        connection.send(reply, result)

    running = threading.BoundedSemaphore(concurrency)
    dependencies: Dependencies = {}
    while (frame := connection.receive()) is not None:
        invoke, payload = frame
        if isinstance(invoke, Dependency):
            dependencies[invoke.task_id] = (invoke.encoding, payload)
            continue
        if not isinstance(invoke, Invoke):
            raise ProtocolError("load after the handler loaded")
        if not running.acquire(blocking=False):
            raise ProtocolError(f"more than {concurrency} attempts at once")

        def worker(
            invoke: Invoke = invoke, payload: bytearray = payload, deps: Dependencies = dependencies
        ) -> None:
            try:
                run(invoke, payload, deps)
            finally:
                running.release()

        threading.Thread(target=worker, name=f"attempt-{invoke.attempt_id}", daemon=True).start()
        dependencies = {}
    return 0


class _Attempts:
    """Runs attempts of one handler with its lifecycle hooks."""

    def __init__(self, handler: Callable[..., Any], hooks: LifecycleHooks, reference: str) -> None:
        self._handler = handler
        self._hooks = hooks
        self._startup = startup_context(reference)

    def run(
        self, invoke: Invoke, payload: bytearray, dependencies: Dependencies
    ) -> tuple[Succeeded | Failed, bytes]:
        """Run the handler between its hooks: on_running; then on_success or
        on_error followed by on_retry or on_failure; then on_finish. Hooks run
        before the outcome is sent, so their output is the attempt's and the
        attempt's deadline covers them."""

        encoding = invoke.input_encoding
        root = invoke.root_task_id or invoke.task_id
        context = LifecycleTaskContext(
            hook=LifecycleHookName.Running,
            task_id=invoke.task_id,
            status=TaskStatus.Running,
            root_task_id=root,
            parent_task_id=invoke.parent_task_id or "",
            workspace_name=self._startup.workspace_name,
            container_id=self._startup.container_id,
            container_hostname=self._startup.container_hostname,
            handler=self._startup.handler,
            attempt_number=invoke.attempt_number,
            max_attempts=invoke.max_attempts,
        )
        started = time.monotonic()
        with task_context(invoke.task_id, root):
            run_task_hooks(self._hooks, LifecycleHookName.Running, context)
            try:
                args, kwargs = _decode_arguments(encoding, payload, dependencies)
                result = invoke_handler(self._handler, args, kwargs, encoding=encoding)
                encoded = _encode_result(result, encoding)
            except (KeyboardInterrupt, ProtocolError):
                raise
            except BaseException as exc:
                self._failed(context, exc, time.monotonic() - started)
                failed = Failed(
                    type="failed", attempt_id=invoke.attempt_id, error=_runner_error(exc)
                )
                return failed, _exception_payload(exc)
            done = context.model_copy(
                update={
                    "status": TaskStatus.Complete,
                    "duration_seconds": time.monotonic() - started,
                    "result_available": True,
                }
            )
            run_task_hooks(self._hooks, LifecycleHookName.Success, done)
            run_task_hooks(self._hooks, LifecycleHookName.Finish, done)
        succeeded = Succeeded(
            type="succeeded", attempt_id=invoke.attempt_id, result_encoding=encoding
        )
        return succeeded, encoded

    def _failed(self, context: LifecycleTaskContext, exc: BaseException, duration: float) -> None:
        failed = context.model_copy(
            update={
                "status": TaskStatus.Failed,
                "duration_seconds": duration,
                "error_type": type(exc).__name__,
                "error_message": _message(exc),
            }
        )
        run_task_hooks(self._hooks, LifecycleHookName.Error, failed)
        # The platform retries a raised exception while attempts remain.
        if context.attempt_number < context.max_attempts:
            final = failed.model_copy(update={"status": TaskStatus.Retry, "retry_scheduled": True})
            run_task_hooks(self._hooks, LifecycleHookName.Retry, final)
        else:
            final = failed
            run_task_hooks(self._hooks, LifecycleHookName.Failure, final)
        run_task_hooks(self._hooks, LifecycleHookName.Finish, final)


def _message(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return f"<unprintable {type(exc).__name__}>"


def _decode_arguments(
    encoding: Encoding,
    payload: bytearray,
    dependencies: Dependencies,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    try:
        if encoding is Encoding.json:
            arguments = Arguments.model_validate_json(payload)
        else:
            unpickler = _DependencyUnpickler(io.BytesIO(payload), dependencies)
            arguments = Arguments.model_validate(unpickler.load())
    except ProtocolError:
        raise
    except Exception as exc:
        raise InvalidInputError(f"invalid {encoding.value} arguments: {exc}") from exc
    return tuple(arguments.args), arguments.kwargs


class _DependencyUnpickler(pickle.Unpickler):
    """Resolves `("function_call", task_id)` to the upstream task's decoded result."""

    def __init__(self, file: io.BytesIO, dependencies: Dependencies) -> None:
        super().__init__(file)
        self._dependencies = dependencies

    def persistent_load(self, pid: Any) -> Any:
        match pid:
            case (str(kind), str(task_id)) if kind == _FUNCTION_CALL:
                pass
            case _:
                raise ProtocolError(f"unsupported persistent id {pid!r}")
        try:
            encoding, payload = self._dependencies[task_id]
        except KeyError:
            raise ProtocolError(f"input refers to task {task_id} without its dependency") from None
        if encoding is Encoding.json:
            return json.loads(payload)
        return pickle.loads(payload)


def _encode_result(result: Any, encoding: Encoding) -> bytes:
    try:
        if encoding is Encoding.json:
            encoded = json.dumps(
                to_json_value(result), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
        else:
            encoded = cloudpickle_bytes(result)
    except Exception as exc:
        raise ValueError(
            f"return value of type {type(result).__name__} cannot be encoded as "
            f"{encoding.value}: {exc}"
        ) from exc
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"encoded return value is {len(encoded)} bytes; the limit is {MAX_PAYLOAD_BYTES}"
        )
    return encoded


def _runner_error(exc: BaseException) -> RunnerError:
    cls = type(exc)
    name = (
        cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"
    )
    try:
        message = str(exc)
    except Exception:
        message = f"<unprintable {name}>"
    formatted = "".join(traceback.format_exception(cls, exc, exc.__traceback__))
    return RunnerError(type=name, message=message, traceback=formatted)


def _exception_payload(exc: BaseException) -> bytes:
    """Cloudpickle the exception when it survives a round trip, else nothing."""

    try:
        encoded = cloudpickle_bytes(exc)
        pickle.loads(encoded)
    except KeyboardInterrupt:
        raise
    except BaseException:
        return b""
    return encoded if len(encoded) <= MAX_PAYLOAD_BYTES else b""


def _flush_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()


def main() -> int:
    # Removed from the environment so user code and its subprocesses never see it.
    raw_fd = os.environ.pop(RUNNER_FD_ENV, "")
    try:
        sock = socket.socket(fileno=int(raw_fd))
    except (ValueError, OSError) as exc:
        print(
            f"lazycloud runner: {RUNNER_FD_ENV}={raw_fd!r} is not a socket descriptor: {exc}",
            file=sys.stderr,
        )
        return PROTOCOL_ERROR_EXIT
    # A subprocess holding the socket would keep it open after the runner exits.
    sock.set_inheritable(False)
    with sock:
        if sock.type != socket.SOCK_STREAM:
            print(f"lazycloud runner: {RUNNER_FD_ENV} is not a stream socket", file=sys.stderr)
            return PROTOCOL_ERROR_EXIT
        try:
            return serve(Connection(sock))
        except (ProtocolError, OSError) as exc:
            _flush_output()
            print(f"lazycloud runner: {exc}", file=sys.stderr, flush=True)
            return PROTOCOL_ERROR_EXIT


__all__ = ["MAX_HEADER_BYTES", "MAX_PAYLOAD_BYTES", "RUNNER_FD_ENV", "Connection", "main", "serve"]
