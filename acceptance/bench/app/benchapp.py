"""The benchmark app, deployed unchanged to the reference and the rewrite."""

from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel

from lazycloud import App, Autoscaler, Image

app = App("lcbench")
image = Image(python_version="3.12")
# The reference runs workloads on a joined machine only when they pin it.
MACHINE = os.environ.get("LCBENCH_MACHINE") or None


@app.function(
    name="echo",
    image=image,
    machine=MACHINE,
    cpu=0.25,
    memory="256Mi",
    concurrency=4,
    keep_warm=60,
    timeout_seconds=3600,
    max_pending_tasks=20000,
    autoscaler=Autoscaler(max_containers=8, tasks_per_container=4),
)
def echo(x: int) -> int:
    return x


@app.function(
    name="hold",
    image=image,
    machine=MACHINE,
    cpu=0.25,
    memory="256Mi",
    keep_warm=60,
    max_pending_tasks=20000,
    timeout_seconds=3600,
)
def hold(x: int) -> int:
    return x


class Pong(BaseModel):
    n: int


@app.endpoint(
    name="ping",
    route="/ping",
    methods=["POST"],
    image=image,
    machine=MACHINE,
    cpu=0.25,
    memory="256Mi",
    concurrency=64,
    keep_warm=1800,
    max_pending_tasks=5000,
    autoscaler=Autoscaler(max_containers=4, tasks_per_container=64),
)
def ping(n: int = 0) -> Pong:
    return Pong(n=n)


# Where the benchmark listens for per-request callbacks; unset in containers.
CALLBACK_URL = os.environ.get("LCBENCH_CALLBACK_URL") or None


@app.endpoint(
    name="pingcb",
    route="/pingcb",
    methods=["POST"],
    image=image,
    machine=MACHINE,
    cpu=0.25,
    memory="256Mi",
    concurrency=64,
    keep_warm=1800,
    max_pending_tasks=5000,
    autoscaler=Autoscaler(max_containers=4, tasks_per_container=64),
    callback_url=CALLBACK_URL,
)
def pingcb(n: int = 0) -> Pong:
    return Pong(n=n)


@app.endpoint(
    name="coldping",
    route="/coldping",
    methods=["POST"],
    image=image,
    machine=MACHINE,
    cpu=0.25,
    memory="256Mi",
    keep_warm=1,
)
def coldping(n: int = 0) -> Pong:
    return Pong(n=n)


EVENTS = 20
INTERVAL = 0.05


async def events(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream"), (b"cache-control", b"no-cache")],
        }
    )
    for i in range(EVENTS):
        await send(
            {"type": "http.response.body", "body": f"data: {i}\n\n".encode(), "more_body": True}
        )
        await asyncio.sleep(INTERVAL)
    await send({"type": "http.response.body", "body": b"", "more_body": False})


stream = app.asgi(
    name="stream",
    image=image,
    machine=MACHINE,
    cpu=0.25,
    memory="256Mi",
    concurrent_requests=64,
    keep_warm_seconds=1800,
)(events)
