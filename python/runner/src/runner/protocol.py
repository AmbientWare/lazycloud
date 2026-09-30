"""Serve the local runner protocol on the socket the supervisor passes in.

Frames are a 4-byte big-endian header length, a UTF-8 JSON header, a 4-byte
big-endian payload length and the payload bytes. The schemas live in
contracts/runner.yaml. Stdout and stderr belong to the user's task output, so
protocol errors are reported on stderr only when the runner is about to exit.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import traceback
from collections.abc import Callable
from typing import Annotated, Any

import cloudpickle
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from shared.errors import InvalidInputError
from shared.serialization import to_json_value
from shared.task_context import task_context

from runner.handler_loading import load_handler
from runner.invocation import invoke_handler
from runner.protocol_models import (
    Encoding,
    Failed,
    Invoke,
    Load,
    Loaded,
    LoadFailed,
    RunnerError,
    Succeeded,
)

RUNNER_FD_ENV = "LAZYCLOUD_RUNNER_FD"
MAX_HEADER_BYTES = 1 << 20
MAX_PAYLOAD_BYTES = 64 << 20
PROTOCOL_ERROR_EXIT = 2
LOAD_FAILED_EXIT = 1

_INBOUND: TypeAdapter[Load | Invoke] = TypeAdapter(
    Annotated[Load | Invoke, Field(discriminator="type")]
)


class ProtocolError(Exception):
    """The supervisor sent a frame the protocol does not allow."""


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class Connection:
    """Frame reads and writes on the supervisor socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def receive(self) -> tuple[Load | Invoke, bytearray] | None:
        """Read one frame, or None when the supervisor closed between frames."""

        prefix = self._read(4, eof_allowed=True)
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
        self._sock.sendall(
            len(encoded).to_bytes(4, "big") + encoded + len(payload).to_bytes(4, "big")
        )
        if payload:
            self._sock.sendall(payload)

    def _read(self, size: int, *, eof_allowed: bool = False) -> bytearray | None:
        buffer = bytearray(size)
        view = memoryview(buffer)
        filled = 0
        while filled < size:
            count = self._sock.recv_into(view[filled:])
            if count == 0:
                if filled == 0 and eof_allowed:
                    return None
                raise ProtocolError("supervisor closed the socket inside a frame")
            filled += count
        return buffer


def serve(connection: Connection) -> int:
    """Load the handler, then run invocations until the supervisor closes."""

    frame = connection.receive()
    if frame is None:
        return 0
    load, _ = frame
    if not isinstance(load, Load):
        raise ProtocolError("expected load as the first frame")
    try:
        handler = load_handler(load.handler)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        _flush_output()
        connection.send(LoadFailed(type="load_failed", error=_runner_error(exc)))
        return LOAD_FAILED_EXIT
    _flush_output()
    connection.send(Loaded(type="loaded"))
    while (frame := connection.receive()) is not None:
        invoke, payload = frame
        if not isinstance(invoke, Invoke):
            raise ProtocolError("load after the handler loaded")
        reply, result = _attempt(handler, invoke, payload)
        _flush_output()
        connection.send(reply, result)
    return 0


def _attempt(
    handler: Callable[..., Any], invoke: Invoke, payload: bytearray
) -> tuple[Succeeded | Failed, bytes]:
    encoding = invoke.input_encoding
    try:
        args, kwargs = _decode_arguments(encoding, payload)
        with task_context(invoke.task_id):
            result = invoke_handler(handler, args, kwargs, encoding=encoding)
        encoded = _encode_result(result, encoding)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        failed = Failed(type="failed", attempt_id=invoke.attempt_id, error=_runner_error(exc))
        return failed, _exception_payload(exc)
    succeeded = Succeeded(type="succeeded", attempt_id=invoke.attempt_id, result_encoding=encoding)
    return succeeded, encoded


def _decode_arguments(
    encoding: Encoding, payload: bytearray
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    try:
        if encoding is Encoding.json:
            arguments = _Arguments.model_validate_json(payload)
        else:
            arguments = _Arguments.model_validate(cloudpickle.loads(payload))
    except Exception as exc:
        raise InvalidInputError(f"invalid {encoding.value} arguments: {exc}") from exc
    return tuple(arguments.args), arguments.kwargs


def _encode_result(result: Any, encoding: Encoding) -> bytes:
    try:
        if encoding is Encoding.json:
            encoded = json.dumps(
                to_json_value(result), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
        else:
            encoded = cloudpickle.dumps(result)
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
        encoded = cloudpickle.dumps(exc)
        cloudpickle.loads(encoded)
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
