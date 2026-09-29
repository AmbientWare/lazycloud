from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import Field
from shared.contracts import ContractModel
from shared.http.pods import PodSandboxResultResponse

from worker.container_service.models import (
    SandboxFilesystemOutput,
    SandboxProcessEvent,
    SandboxProcessEventType,
    WorkerContainerServiceInstance,
    WorkerSandboxProcess,
)
from worker.container_service.protocols import WorkerSandboxProcessManager
from worker.sandbox_server import SandboxLogStream

SANDBOX_SUPERVISOR_PROTOCOL_VERSION = 1
SANDBOX_SUPERVISOR_PORT = 7111
DEFAULT_SUPERVISOR_READY_TIMEOUT_SECONDS = 30.0
DEFAULT_SUPERVISOR_RECONNECT_TIMEOUT_SECONDS = 30.0
DEFAULT_SUPERVISOR_DRAIN_TIMEOUT_SECONDS = 5.0
SUPERVISOR_WITHOUT_FILESYSTEM_MESSAGE = (
    "this container's sandbox supervisor is too old to read or write its files; "
    "restart the devbox or container to update it"
)


class SandboxSupervisorError(RuntimeError):
    pass


class SupervisorRequest(ContractModel):
    version: Literal[1] = SANDBOX_SUPERVISOR_PROTOCOL_VERSION
    op: str
    argv: list[str] = Field(default_factory=list)
    cwd: str = ""
    env: list[str] = Field(default_factory=list)
    pid: int = 0
    process_id: str = ""
    wait_seconds: float = 0
    signal: int = 0
    ack_seq: int = 0
    ok: bool = False
    token: str = Field(default="", repr=False)
    payload: str = ""


class SupervisorProcess(ContractModel):
    pid: int
    process_id: str
    command: str
    cwd: str = ""
    running: bool = True
    exit_code: int = -1


class SupervisorResponse(ContractModel):
    version: int
    type: str
    error: str = ""
    pid: int = 0
    seq: int = 0
    stream: str = ""
    data: str = ""
    exit_code: int = 0
    running: bool = False
    processes: list[SupervisorProcess] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    process_id: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    expires_at: datetime | None = None


@dataclass(slots=True)
class SupervisorSandboxProcessManagerFactory:
    port: int = SANDBOX_SUPERVISOR_PORT
    ready_timeout_seconds: float = DEFAULT_SUPERVISOR_READY_TIMEOUT_SECONDS
    reconnect_timeout_seconds: float = DEFAULT_SUPERVISOR_RECONNECT_TIMEOUT_SECONDS
    drain_timeout_seconds: float = DEFAULT_SUPERVISOR_DRAIN_TIMEOUT_SECONDS
    _connections: _SupervisorConnectionCoordinator = field(
        default_factory=lambda: _SupervisorConnectionCoordinator(),
        init=False,
    )

    def create_process_manager(
        self,
        instance: WorkerContainerServiceInstance,
    ) -> WorkerSandboxProcessManager:
        if not instance.container_ip:
            raise SandboxSupervisorError("sandbox supervisor address is unavailable")
        token = _read_supervisor_token(instance)
        return SupervisorSandboxProcessManager(
            host=instance.container_ip,
            token=token,
            port=self.port,
            ready_timeout_seconds=self.ready_timeout_seconds,
            reconnect_timeout_seconds=self.reconnect_timeout_seconds,
            connections=self._connections,
        )

    def suspend_process_streams(self, instance: WorkerContainerServiceInstance) -> None:
        if instance.container_ip:
            self._connections.suspend(instance.container_ip)
            _drain_supervisor_connections(
                instance.container_ip,
                self.port,
                token=_read_supervisor_token(instance),
                timeout_seconds=self.drain_timeout_seconds,
            )

    def resume_process_streams(self, instance: WorkerContainerServiceInstance) -> None:
        if instance.container_ip:
            self._connections.resume(instance.container_ip)


@dataclass(slots=True)
class SupervisorSandboxProcessManager:
    host: str
    token: str = field(repr=False)
    port: int = SANDBOX_SUPERVISOR_PORT
    ready_timeout_seconds: float = DEFAULT_SUPERVISOR_READY_TIMEOUT_SECONDS
    reconnect_timeout_seconds: float = DEFAULT_SUPERVISOR_RECONNECT_TIMEOUT_SECONDS
    connections: _SupervisorConnectionCoordinator = field(
        default_factory=lambda: _SupervisorConnectionCoordinator(),
    )
    _transport: _SupervisorTransport | None = field(default=None, init=False)
    _pid: int = field(default=0, init=False)
    _process_id: str = field(default="", init=False)
    _ack_seq: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def ready(self) -> bool:
        response = self._request(SupervisorRequest(op="ready"), wait_ready=True)
        return response.type == "ready"

    def start_workload(self) -> None:
        response = self._request(SupervisorRequest(op="start-workload"))
        if response.type != "started":
            raise SandboxSupervisorError("supervisor did not start the workload")

    def stream_exec(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: list[str],
    ) -> Iterable[SandboxProcessEvent]:
        request = SupervisorRequest(op="exec", argv=argv, cwd=cwd, env=env)
        reconnect_deadline = time.monotonic() + self.reconnect_timeout_seconds
        while True:
            try:
                transport = self._connect(wait_ready=self._pid == 0)
                transport.send(
                    request
                    if self._pid == 0
                    else SupervisorRequest(
                        op="watch", process_id=self._process_id, ack_seq=self._ack_seq
                    )
                )
                while response := transport.receive():
                    self._validate(response)
                    if response.type == "started":
                        if not response.process_id:
                            raise SandboxSupervisorError("supervisor did not identify the command")
                        self._pid = response.pid
                        self._process_id = response.process_id
                        yield SandboxProcessEvent(
                            event_type=SandboxProcessEventType.Started,
                            pid=response.pid,
                            process_id=response.process_id,
                        )
                    elif response.type == "chunk":
                        yield SandboxProcessEvent(
                            event_type=SandboxProcessEventType.Chunk,
                            pid=response.pid,
                            seq=response.seq,
                            stream=(
                                SandboxLogStream.Stderr
                                if response.stream == "stderr"
                                else SandboxLogStream.Stdout
                            ),
                            data=base64.b64decode(response.data),
                        )
                    elif response.type == "exited":
                        yield SandboxProcessEvent(
                            event_type=SandboxProcessEventType.Exited,
                            pid=response.pid,
                            exit_code=response.exit_code,
                        )
                        return
                    elif response.type == "error":
                        raise SandboxSupervisorError(response.error or "supervisor stream failed")
                raise ConnectionError("sandbox supervisor stream closed")
            except (ConnectionError, OSError, SandboxSupervisorError):
                self.cleanup()
                if self._pid == 0 or time.monotonic() >= reconnect_deadline:
                    raise
                time.sleep(0.05)

    def ack(self, pid: int, seq: int, *, ok: bool) -> None:
        with self._lock:
            if pid != self._pid or self._transport is None:
                raise SandboxSupervisorError("sandbox supervisor stream is not connected")
            self._transport.send(SupervisorRequest(op="ack", pid=pid, ack_seq=seq, ok=ok))
            if ok:
                self._ack_seq = max(self._ack_seq, seq)

    def run_filesystem(self, payload: str) -> SandboxFilesystemOutput:
        """Run the file helper outside the process log and collect all of its output."""
        transport = _SupervisorTransport.connect(
            self.host,
            self.port,
            token=self.token,
            timeout_seconds=5.0,
            retry=False,
            coordinator=self.connections,
        )
        transport.connection.settimeout(None)
        stdout = bytearray()
        try:
            transport.send(SupervisorRequest(op="filesystem", payload=payload))
            while response := transport.receive():
                self._validate(response)
                if response.type == "chunk":
                    stdout.extend(base64.b64decode(response.data))
                elif response.type == "exited":
                    return SandboxFilesystemOutput(
                        stdout=bytes(stdout),
                        stderr=response.stderr.encode(),
                        exit_code=response.exit_code,
                    )
                elif response.type == "error":
                    if response.error == "unknown operation":
                        raise SandboxSupervisorError(SUPERVISOR_WITHOUT_FILESYSTEM_MESSAGE)
                    raise SandboxSupervisorError(response.error or "sandbox file operation failed")
            raise SandboxSupervisorError("sandbox supervisor ended a file operation early")
        finally:
            transport.close()

    def status(self, pid: int) -> int | None:
        response = self._request(SupervisorRequest(op="status", pid=pid))
        return None if response.running else response.exit_code

    async def result(self, process_id: str, wait_seconds: float) -> PodSandboxResultResponse:
        async with asyncio.timeout(wait_seconds + 2):
            while True:
                await self.connections.wait_until_active_async(self.host)
                # Both retained streams can expand sixfold when JSON escapes control bytes.
                reader, writer = await asyncio.open_connection(
                    self.host, self.port, limit=4 * 1024 * 1024
                )
                connection = _AsyncSupervisorConnection(asyncio.get_running_loop(), writer)
                registered = self.connections.register(self.host, connection)
                try:
                    if not registered:
                        continue
                    request = SupervisorRequest(
                        op="result",
                        process_id=process_id,
                        wait_seconds=wait_seconds,
                        token=self.token,
                    )
                    writer.write(request.model_dump_json().encode() + b"\n")
                    await writer.drain()
                    line = await reader.readline()
                    if not line:
                        raise SandboxSupervisorError("sandbox supervisor returned no response")
                    response = SupervisorResponse.model_validate_json(line)
                    self._validate(response)
                    if response.type == "error":
                        raise SandboxSupervisorError(response.error or "sandbox result unavailable")
                    break
                finally:
                    self.connections.unregister(self.host, connection)
                    writer.close()
                    with suppress(OSError):
                        await writer.wait_closed()
        if response.type != "result" or response.process_id != process_id:
            raise SandboxSupervisorError("supervisor returned a different command result")
        return PodSandboxResultResponse(
            process_id=response.process_id,
            pid=response.pid,
            running=response.running,
            exit_code=response.exit_code,
            stdout=response.stdout,
            stderr=response.stderr,
            stdout_truncated=response.stdout_truncated,
            stderr_truncated=response.stderr_truncated,
            expires_at=response.expires_at,
        )

    def stdout(self, pid: int) -> str:
        return self._request(SupervisorRequest(op="stdout", pid=pid)).stdout

    def stderr(self, pid: int) -> str:
        return self._request(SupervisorRequest(op="stderr", pid=pid)).stderr

    def kill(self, pid: int, process_id: str = "") -> None:
        self._request(SupervisorRequest(op="kill", pid=pid, process_id=process_id, signal=15))

    def list_processes(self) -> list[WorkerSandboxProcess]:
        response = self._request(SupervisorRequest(op="list"))
        return [
            WorkerSandboxProcess(
                pid=process.pid,
                process_id=process.process_id,
                command=process.command,
                cwd=process.cwd,
                exit_code=process.exit_code,
                running=process.running,
            )
            for process in response.processes
        ]

    def cleanup(self) -> None:
        with self._lock:
            if self._transport is not None:
                self._transport.close()
                self._transport = None

    def _request(
        self,
        request: SupervisorRequest,
        *,
        wait_ready: bool = False,
        timeout_seconds: float = 5.0,
    ) -> SupervisorResponse:
        transport = _SupervisorTransport.connect(
            self.host,
            self.port,
            token=self.token,
            timeout_seconds=(self.ready_timeout_seconds if wait_ready else timeout_seconds),
            retry=wait_ready,
            coordinator=self.connections,
        )
        try:
            transport.send(request)
            response = transport.receive()
            if response is None:
                raise SandboxSupervisorError("sandbox supervisor returned no response")
            self._validate(response)
            if response.type == "error":
                raise SandboxSupervisorError(response.error or "sandbox supervisor request failed")
            return response
        finally:
            transport.close()

    def _connect(self, *, wait_ready: bool) -> _SupervisorTransport:
        with self._lock:
            if self._transport is not None:
                self._transport.close()
                self._transport = None
            self._transport = _SupervisorTransport.connect(
                self.host,
                self.port,
                token=self.token,
                timeout_seconds=(self.ready_timeout_seconds if wait_ready else 5.0),
                retry=wait_ready,
                coordinator=self.connections,
            )
            self._transport.connection.settimeout(None)
            return self._transport

    def _validate(self, response: SupervisorResponse) -> None:
        if response.version != SANDBOX_SUPERVISOR_PROTOCOL_VERSION:
            raise SandboxSupervisorError(
                f"sandbox supervisor protocol mismatch: {response.version}"
            )


@dataclass(slots=True)
class _SupervisorTransport:
    connection: socket.socket
    reader: BinaryIO
    host: str
    coordinator: _SupervisorConnectionCoordinator
    token: str = field(repr=False)
    _closed: bool = field(default=False, init=False)

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        token: str,
        timeout_seconds: float,
        retry: bool,
        coordinator: _SupervisorConnectionCoordinator,
    ) -> _SupervisorTransport:
        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        last_error: OSError | None = None
        while True:
            coordinator.wait_until_active(host)
            try:
                connection = socket.create_connection(
                    (host, port), timeout=max(deadline - time.monotonic(), 0.1)
                )
                connection.settimeout(max(timeout_seconds, 0.1))
                if not coordinator.register(host, connection):
                    connection.close()
                    continue
                return cls(
                    connection=connection,
                    reader=connection.makefile("rb"),
                    host=host,
                    coordinator=coordinator,
                    token=token,
                )
            except OSError as exc:
                last_error = exc
                if not retry or time.monotonic() >= deadline:
                    raise SandboxSupervisorError(
                        f"sandbox supervisor at {host}:{port} is unavailable: {last_error}"
                    ) from exc
                time.sleep(0.05)

    def send(self, request: SupervisorRequest) -> None:
        payload = request.model_copy(update={"token": self.token}).model_dump(mode="json")
        self.connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")

    def receive(self) -> SupervisorResponse | None:
        line = self.reader.readline()
        if not line:
            return None
        return SupervisorResponse.model_validate_json(line)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.coordinator.unregister(self.host, self.connection)
        try:
            self.reader.close()
        finally:
            self.connection.close()


@dataclass(slots=True, eq=False)
class _AsyncSupervisorConnection:
    loop: asyncio.AbstractEventLoop
    writer: asyncio.StreamWriter

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.writer.close)


@dataclass(slots=True)
class _SupervisorConnectionCoordinator:
    _condition: threading.Condition = field(default_factory=threading.Condition)
    _suspension_counts: dict[str, int] = field(default_factory=dict)
    _connections: dict[str, set[socket.socket | _AsyncSupervisorConnection]] = field(
        default_factory=dict
    )
    _async_waiters: dict[str, set[asyncio.Future[None]]] = field(default_factory=dict)

    def wait_until_active(self, host: str) -> None:
        with self._condition:
            while self._suspension_counts.get(host, 0) > 0:
                self._condition.wait()

    async def wait_until_active_async(self, host: str) -> None:
        while True:
            with self._condition:
                if self._suspension_counts.get(host, 0) == 0:
                    return
                waiter = asyncio.get_running_loop().create_future()
                self._async_waiters.setdefault(host, set()).add(waiter)
            try:
                await waiter
            finally:
                with self._condition:
                    waiters = self._async_waiters.get(host)
                    if waiters is not None:
                        waiters.discard(waiter)
                        if not waiters:
                            del self._async_waiters[host]

    def register(self, host: str, connection: socket.socket | _AsyncSupervisorConnection) -> bool:
        with self._condition:
            if self._suspension_counts.get(host, 0) > 0:
                return False
            self._connections.setdefault(host, set()).add(connection)
            return True

    def unregister(self, host: str, connection: socket.socket | _AsyncSupervisorConnection) -> None:
        with self._condition:
            connections = self._connections.get(host)
            if connections is None:
                return
            connections.discard(connection)
            if not connections:
                self._connections.pop(host, None)

    def suspend(self, host: str) -> None:
        with self._condition:
            self._suspension_counts[host] = self._suspension_counts.get(host, 0) + 1
            connections = tuple(self._connections.pop(host, set()))
        for connection in connections:
            if isinstance(connection, socket.socket):
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                connection.close()

    def resume(self, host: str) -> None:
        with self._condition:
            remaining = self._suspension_counts.get(host, 0) - 1
            if remaining > 0:
                self._suspension_counts[host] = remaining
                return
            self._suspension_counts.pop(host, None)
            self._condition.notify_all()
            for waiter in self._async_waiters.pop(host, ()):
                waiter.get_loop().call_soon_threadsafe(_resume_supervisor_waiter, waiter)


def _resume_supervisor_waiter(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


def _drain_supervisor_connections(
    host: str,
    port: int,
    *,
    token: str,
    timeout_seconds: float,
) -> None:
    transport = _SupervisorTransport.connect(
        host,
        port,
        token=token,
        timeout_seconds=timeout_seconds,
        retry=True,
        coordinator=_SupervisorConnectionCoordinator(),
    )
    transport.connection.settimeout(timeout_seconds)
    try:
        transport.send(SupervisorRequest(op="drain"))
        response = transport.receive()
        if response is None:
            raise SandboxSupervisorError("sandbox supervisor drain returned no response")
        if response.version != SANDBOX_SUPERVISOR_PROTOCOL_VERSION:
            raise SandboxSupervisorError(
                f"sandbox supervisor protocol mismatch: {response.version}"
            )
        if response.type == "error":
            raise SandboxSupervisorError(response.error or "sandbox supervisor drain failed")
        if response.type != "drained":
            raise SandboxSupervisorError(
                f"sandbox supervisor returned unexpected drain response: {response.type}"
            )
        if transport.receive() is not None:
            raise SandboxSupervisorError("sandbox supervisor drain stream remained open")
    except TimeoutError as exc:
        raise SandboxSupervisorError("sandbox supervisor drain timed out") from exc
    finally:
        transport.close()


def _read_supervisor_token(instance: WorkerContainerServiceInstance) -> str:
    path = Path(instance.sandbox_supervisor_token_path)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SandboxSupervisorError("sandbox supervisor credential is unavailable") from exc
    if len(token) < 32:
        raise SandboxSupervisorError("sandbox supervisor credential is invalid")
    return token


__all__ = [
    "SANDBOX_SUPERVISOR_PORT",
    "SANDBOX_SUPERVISOR_PROTOCOL_VERSION",
    "SandboxSupervisorError",
    "SupervisorSandboxProcessManagerFactory",
]
