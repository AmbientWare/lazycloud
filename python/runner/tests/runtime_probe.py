"""Drive `python3 -m runner` from the managed runtime inside a plain Python image.

Imports only the standard library so it runs before anything from the runtime
is known to work. Exits non-zero with the reason when any reply differs.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
from collections.abc import Mapping

PROBE_APP = """
import platform

import lazycloud

app = lazycloud.App("runtime_probe")


@app.function()
def probe(count: int) -> dict:
    return {
        "task": lazycloud.current_task_id(),
        "count": count,
        "python": platform.python_version(),
    }
"""


def send(sock: socket.socket, header: Mapping[str, object], payload: bytes = b"") -> None:
    encoded = json.dumps(header).encode()
    sock.sendall(
        len(encoded).to_bytes(4, "big") + encoded + len(payload).to_bytes(4, "big") + payload
    )


def receive(sock: socket.socket) -> tuple[dict[str, object], bytes]:
    header = read(sock, int.from_bytes(read(sock, 4), "big"))
    payload = read(sock, int.from_bytes(read(sock, 4), "big"))
    return json.loads(header), payload


def read(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise SystemExit("runner closed the socket")
        data += chunk
    return bytes(data)


def main() -> int:
    with tempfile.TemporaryDirectory() as workdir:
        with open(os.path.join(workdir, "probe_app.py"), "w", encoding="utf-8") as file:
            file.write(PROBE_APP)
        parent, child = socket.socketpair()
        parent.settimeout(60)
        process = subprocess.Popen(
            [sys.executable, "-m", "runner"],
            cwd=workdir,
            pass_fds=[child.fileno()],
            env={**os.environ, "LAZYCLOUD_RUNNER_FD": str(child.fileno())},
        )
        child.close()
        with parent:
            send(parent, {"type": "load", "protocol_version": 1, "handler": "probe_app:probe"})
            loaded, _ = receive(parent)
            if loaded != {"type": "loaded"}:
                raise SystemExit(f"unexpected load reply: {loaded}")
            invoke = {
                "type": "invoke",
                "task_id": "probe-task",
                "attempt_id": "probe-attempt",
                "input_encoding": "json",
            }
            send(parent, invoke, json.dumps({"args": ["3"], "kwargs": {}}).encode())
            reply, payload = receive(parent)
        code = process.wait(timeout=60)
    expected: dict[str, object] = {
        "task": "probe-task",
        "count": 3,
        "python": platform.python_version(),
    }
    if reply.get("type") != "succeeded" or json.loads(payload) != expected or code != 0:
        raise SystemExit(f"unexpected invoke reply: {reply} {payload!r} exit={code}")
    print(f"runtime probe ok: python {expected['python']} task {expected['task']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
