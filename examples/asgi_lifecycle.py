"""Production acceptance app for ASGI HTTP and WebSocket lifecycle behavior.

Deploy from the repository root with a disposable app name:

    LAZYCLOUD_ASGI_LIFECYCLE_APP_NAME=asgi-lifecycle-acceptance \
        uv run lazycloud deploy examples.asgi_lifecycle:app --workspace default
"""

from __future__ import annotations

import asyncio
import os

from examples.asgi import ASGIMessage, ASGIReceive, ASGISend
from lazycloud import App, Image

APP_NAME = os.getenv("LAZYCLOUD_ASGI_LIFECYCLE_APP_NAME", "asgi_lifecycle")

app = App(APP_NAME)


@app.asgi(
    name="streaming-service",
    route="/streaming-service",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    keep_warm_seconds=0,
)
async def streaming_service(
    scope: ASGIMessage,
    receive: ASGIReceive,
    send: ASGISend,
) -> None:
    scope_type = scope.get("type")
    if scope_type == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    if scope_type == "websocket":
        await receive()
        offered = scope.get("subprotocols", [])
        subprotocol = "events.v1" if "events.v1" in offered else None
        await send({"type": "websocket.accept", "subprotocol": subprotocol})
        message = await receive()
        if "bytes" in message and message["bytes"] is not None:
            await send({"type": "websocket.send", "bytes": b"echo:" + message["bytes"]})
        else:
            await send(
                {
                    "type": "websocket.send",
                    "text": "echo:" + str(message.get("text", "")),
                }
            )
        await send({"type": "websocket.close", "code": 4001, "reason": "stream complete"})
        return

    path = str(scope.get("path", "/"))
    if path.endswith("/fail"):
        raise RuntimeError("intentional ASGI acceptance failure")

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"first\n", "more_body": True})
    await asyncio.sleep(0.4)
    await send({"type": "http.response.body", "body": b"second\n"})
