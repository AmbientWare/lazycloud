"""Disposable Pod lifecycle and ingress acceptance app."""

from __future__ import annotations

import asyncio
import json
import os

import uvicorn

from examples.asgi import ASGIMessage, ASGIReceive, ASGISend
from lazycloud import App, Image

APP_NAME = os.getenv("LAZYCLOUD_POD_ACCEPTANCE_APP", "pod_lifecycle_acceptance")
MARKER = os.getenv("LAZYCLOUD_POD_ACCEPTANCE_MARKER", "v1")

app = App(APP_NAME)
image = Image(python_version="3.12")

default_web = app.pod(
    name="default-web",
    image=image,
    command=["python", "-m", "examples.pod_lifecycle"],
    ports={"http": 8080, "tcp": 9090},
    env={"POD_ACCEPTANCE_MARKER": MARKER},
    tcp=True,
)

always_on = app.pod(
    name="always-on",
    image=image,
    command=["python", "-m", "examples.pod_lifecycle"],
    ports={"http": 8080, "tcp": 9090},
    env={"POD_ACCEPTANCE_MARKER": MARKER},
    cpu=0.25,
    memory="128Mi",
    keep_warm=-1,
    tcp=True,
)


async def pod_service(scope: ASGIMessage, receive: ASGIReceive, send: ASGISend) -> None:
    scope_type = scope.get("type")
    if scope_type == "http":
        body = json.dumps(
            {
                "marker": os.getenv("POD_ACCEPTANCE_MARKER", "unknown"),
                "path": scope.get("path", "/"),
                "protocol": "http",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
        return

    if scope_type != "websocket":
        return
    message = await receive()
    if message["type"] != "websocket.connect":
        return
    offered = scope.get("subprotocols") or []
    selected = "pod.echo.v1" if "pod.echo.v1" in offered else None
    await send({"type": "websocket.accept", "subprotocol": selected})
    while True:
        message = await receive()
        message_type = message["type"]
        if message_type == "websocket.disconnect":
            return
        if message_type != "websocket.receive":
            continue
        text = message.get("text")
        if text == "close":
            await send({"type": "websocket.close", "code": 1000, "reason": "requested"})
            return
        if text is not None:
            await send({"type": "websocket.send", "text": f"{MARKER}:{text}"})
            continue
        data = message.get("bytes")
        if data is not None:
            await send({"type": "websocket.send", "bytes": data})


async def _tcp_echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def _serve() -> None:
    tcp_server = await asyncio.start_server(_tcp_echo, "0.0.0.0", 9090)
    http_server = uvicorn.Server(
        uvicorn.Config(
            pod_service,
            host="0.0.0.0",
            port=8080,
            log_level="info",
        )
    )
    async with tcp_server:
        await http_server.serve()


def create_ephemeral(timeout_seconds: int = 30) -> dict[str, str | int]:
    instance = default_web.create(timeout_seconds=timeout_seconds)
    return {
        "container_id": instance.container_id,
        "stub_id": instance.stub_id,
        "url": instance.url,
        "timeout_seconds": instance.timeout_seconds,
    }


if __name__ == "__main__":
    asyncio.run(_serve())


__all__ = ["always_on", "app", "create_ephemeral", "default_web", "pod_service"]
