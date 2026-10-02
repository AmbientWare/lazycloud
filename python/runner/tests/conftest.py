from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


class RunnerClient:
    """The supervisor side of one `python -m runner` process."""

    def __init__(self, workdir: Path) -> None:
        self.sock, child = socket.socketpair()
        self.sock.settimeout(30)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "runner"],
            cwd=workdir,
            pass_fds=[child.fileno()],
            env={**os.environ, "LAZYCLOUD_RUNNER_FD": str(child.fileno())},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        child.close()

    def send(self, header: dict[str, Any], payload: bytes = b"") -> None:
        self.send_raw(json.dumps(header).encode(), payload)

    def send_raw(self, header: bytes, payload: bytes = b"") -> None:
        self.sock.sendall(
            len(header).to_bytes(4, "big") + header + len(payload).to_bytes(4, "big") + payload
        )

    def receive(self) -> tuple[dict[str, Any], bytes]:
        header = self._read(int.from_bytes(self._read(4), "big"))
        payload = self._read(int.from_bytes(self._read(4), "big"))
        return json.loads(header), payload

    def load(self, handler: str) -> dict[str, Any]:
        self.send({"type": "load", "protocol_version": 1, "handler": handler})
        header, payload = self.receive()
        assert payload == b""
        return header

    def invoke(
        self,
        payload: bytes,
        *,
        encoding: str = "json",
        task_id: str = "task-1",
        root_task_id: str = "",
        dependencies: dict[str, tuple[str, bytes]] | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        for upstream, (dependency_encoding, result) in (dependencies or {}).items():
            self.send(
                {"type": "dependency", "task_id": upstream, "encoding": dependency_encoding},
                result,
            )
        self.send(
            {
                "type": "invoke",
                "task_id": task_id,
                "root_task_id": root_task_id or task_id,
                "attempt_id": f"{task_id}-attempt",
                "input_encoding": encoding,
            },
            payload,
        )
        return self.receive()

    def close(self) -> tuple[int, str, str]:
        """Close the socket and collect the exit code and task output."""

        self.sock.close()
        stdout, stderr = self.process.communicate(timeout=30)
        return self.process.returncode, stdout.decode(), stderr.decode()

    def _read(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise EOFError("runner closed the socket")
            data += chunk
        return bytes(data)


@pytest.fixture
def start_runner() -> Iterator[Callable[[Path], RunnerClient]]:
    clients: list[RunnerClient] = []

    def start(workdir: Path) -> RunnerClient:
        client = RunnerClient(workdir)
        clients.append(client)
        return client

    yield start
    for client in clients:
        client.sock.close()
        if client.process.poll() is None:
            client.process.kill()
        client.process.communicate(timeout=30)
