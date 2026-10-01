from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.sync.client import unix_connect

HANDLERS = """
import asyncio
import dataclasses

import lazycloud
from pydantic import BaseModel

app = lazycloud.App("demo")


class WordCount(BaseModel):
    words: int


@app.endpoint(name="count", route="/word-count", methods=["POST"])
def count(text: str, scale: float = 1.0) -> WordCount:
    return WordCount(words=int(len(text.split()) * scale))


@app.endpoint()
def shapes(kind: str):
    if kind == "text":
        return "hello"
    if kind == "bytes":
        return b"\\x00\\x01"
    if kind == "created":
        return {"made": True}, 201, {"X-Made": "yes"}
    if kind == "pair":
        return ("a", "b")
    if kind == "informational":
        return "early", 103
    if kind == "boom":
        raise RuntimeError("exploded")
    return None


@app.endpoint()
async def later(value: int) -> dict:
    await asyncio.sleep(0)
    return {"value": value}


async def stream(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            await send({"type": message["type"] + ".complete"})
            if message["type"] == "lifespan.shutdown":
                return
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            await send({"type": "websocket.send", "text": "echo:" + message["text"]})
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/event-stream")],
    })
    for n in range(3):
        chunk = f"data: {n}\\n\\n".encode()
        await send({"type": "http.response.body", "body": chunk, "more_body": True})
    await send({"type": "http.response.body", "body": b""})


service = app.asgi(name="service")(stream)


@app.realtime(name="talk")
def talk(message: str):
    if message == "many":
        return (word for word in ["a", "b", "c"])
    return {"heard": message}


async def failing_app(scope, receive, send):
    message = await receive()
    if message["type"] == "lifespan.startup":
        await send({"type": "lifespan.startup.failed", "message": "no database"})
"""


class HttpRunner:
    """The supervisor side of one runner serving HTTP on a Unix socket."""

    def __init__(self, workdir: Path, handler: str, kind: str) -> None:
        self.path = str(workdir / f"{kind}.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.path)
        listener.listen()
        self.sock, child = socket.socketpair()
        self.sock.settimeout(30)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "runner"],
            cwd=workdir,
            pass_fds=[child.fileno(), listener.fileno()],
            env={
                **os.environ,
                "LAZYCLOUD_RUNNER_FD": str(child.fileno()),
                "LAZYCLOUD_HTTP_FD": str(listener.fileno()),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        child.close()
        listener.close()
        load: dict[str, object] = {
            "type": "load",
            "protocol_version": 1,
            "handler": handler,
            "http": {"kind": kind, "concurrency": 4},
        }
        header = json.dumps(load).encode()
        self.sock.sendall(len(header).to_bytes(4, "big") + header + (0).to_bytes(4, "big"))
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=self.path), base_url="http://endpoint"
        )

    def reply(self) -> dict[str, Any]:
        size = int.from_bytes(self._read(4), "big")
        header = json.loads(self._read(size))
        assert int.from_bytes(self._read(4), "big") == 0
        return header

    def close(self) -> tuple[int, str, str]:
        self.client.close()
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
def workdir() -> Iterator[Path]:
    # Unix socket paths are limited to 107 bytes, and tmp_path can be longer.
    short = Path(tempfile.mkdtemp(prefix="lcrun-", dir="/tmp"))
    (short / "handlers.py").write_text(HANDLERS, encoding="utf-8")
    yield short
    shutil.rmtree(short)


@pytest.fixture
def serve(workdir: Path) -> Iterator[Any]:
    runners: list[HttpRunner] = []

    def start(handler: str, kind: str) -> HttpRunner:
        runner = HttpRunner(workdir, handler, kind)
        runners.append(runner)
        return runner

    yield start
    for runner in runners:
        runner.client.close()
        runner.sock.close()
        if runner.process.poll() is None:
            runner.process.kill()
        runner.process.communicate(timeout=30)


def test_endpoint_maps_body_and_query_to_arguments_and_models_to_json(serve: Any) -> None:
    runner = serve("handlers:count", "endpoint")
    assert runner.reply() == {"type": "loaded"}

    response = runner.client.post("/word-count", json={"text": "run python on lazycloud"})
    assert (response.status_code, response.json()) == (200, {"words": 4})
    assert response.headers["content-type"] == "application/json"

    # args/kwargs keys are used as given, and query values join the kwargs.
    response = runner.client.post(
        "/word-count", params={"scale": "2"}, json={"args": ["a b"], "kwargs": {}}
    )
    assert response.json() == {"words": 4}

    assert runner.client.post("/", content=b"[1]").json() == {
        "error": "request payload must be a JSON object"
    }
    assert runner.client.post("/", content=b"{").status_code == 400
    code, _, _ = runner.close()
    assert code == 0


@pytest.mark.parametrize(
    ("kind", "status", "content_type", "body"),
    [
        ("text", 200, "text/plain; charset=utf-8", b"hello"),
        ("bytes", 200, "application/octet-stream", b"\x00\x01"),
        ("created", 201, "application/json", b'{"made": true}'),
        ("pair", 200, "application/json", b'["a", "b"]'),
        (
            "informational",
            502,
            "application/json",
            b'{"error": "endpoint returned an informational response without a final response"}',
        ),
        ("none", 200, "application/json", b"null"),
        ("boom", 500, "application/json", b'{"error": "RuntimeError: exploded"}'),
    ],
)
def test_endpoint_results_map_to_responses(
    serve: Any, kind: str, status: int, content_type: str, body: bytes
) -> None:
    runner = serve("handlers:shapes", "endpoint")
    runner.reply()
    response = runner.client.get("/", params={"kind": kind})
    assert (response.status_code, response.headers["content-type"], response.content) == (
        status,
        content_type,
        body,
    )
    if kind == "created":
        assert response.headers["x-made"] == "yes"


def test_async_endpoint_is_awaited(serve: Any) -> None:
    runner = serve("handlers:later", "endpoint")
    runner.reply()
    assert runner.client.post("/", json={"value": "7"}).json() == {"value": 7}


def test_asgi_streams_events_and_upgrades_websockets(serve: Any) -> None:
    runner = serve("handlers:service", "asgi")
    assert runner.reply() == {"type": "loaded"}

    with runner.client.stream("GET", "/events") as response:
        chunks = list(response.iter_text())
    assert "".join(chunks) == "data: 0\n\ndata: 1\n\ndata: 2\n\n"

    with unix_connect(runner.path, "ws://endpoint/ws") as ws:
        ws.send("hi")
        assert ws.recv(timeout=10) == "echo:hi"


def test_realtime_answers_each_message_and_iterables_send_several(serve: Any) -> None:
    runner = serve("handlers:talk", "realtime")
    runner.reply()
    with unix_connect(runner.path, "ws://endpoint/") as ws:
        ws.send("hello")
        assert json.loads(ws.recv(timeout=10)) == {"heard": "hello"}
        ws.send("many")
        assert [ws.recv(timeout=10) for _ in range(3)] == ["a", "b", "c"]


def test_failed_startup_reports_load_failed(serve: Any) -> None:
    runner = serve("handlers:failing_app", "asgi")
    reply = runner.reply()
    assert reply["type"] == "load_failed"
    assert reply["error"]["type"] == "StartupFailed"
