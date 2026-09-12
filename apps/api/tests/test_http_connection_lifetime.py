from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from time import monotonic

import httpx
import pytest
import uvicorn
from api.server import http_protocol
from api.server.http_protocol import BoundedHttpProtocol
from fastapi import FastAPI, Request, WebSocket
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect


def test_connection_age_retires_hot_http_without_interrupting_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_protocol, "HTTP_CONNECTION_MAX_AGE_SECONDS", 0.2)
    asyncio.run(_exercise_connection_lifetime())


async def _exercise_connection_lifetime() -> None:
    app = FastAPI()

    @app.get("/")
    async def connection(request: Request) -> int:
        assert request.client is not None
        return request.client.port

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"start"
            await asyncio.sleep(0.5)
            yield b"done!"

        return StreamingResponse(body(), headers={"content-length": "10"})

    @app.websocket("/socket")
    async def websocket(socket: WebSocket) -> None:
        await socket.accept()
        try:
            while True:
                await socket.send_text(await socket.receive_text())
        except WebSocketDisconnect:
            pass

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                http=BoundedHttpProtocol,
                lifespan="off",
                access_log=False,
                log_level="warning",
                timeout_keep_alive=10,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))

        async def hot_http() -> None:
            ports: list[int] = []
            async with httpx.AsyncClient() as client:
                started = monotonic()
                while monotonic() - started < 0.6:
                    response = await client.get(f"http://127.0.0.1:{port}/")
                    response.raise_for_status()
                    ports.append(int(response.text))
                    await asyncio.sleep(0.01)
            assert ports[0] == ports[1]
            assert len(set(ports)) >= 2

        async def active_response() -> None:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
                assert headers.startswith(b"HTTP/1.1 200")
                assert await asyncio.wait_for(reader.readexactly(10), timeout=2) == b"startdone!"
                assert await asyncio.wait_for(reader.read(1), timeout=2) == b""
            finally:
                writer.close()
                await writer.wait_closed()

        async def upgraded_socket() -> None:
            async with connect(f"ws://127.0.0.1:{port}/socket") as websocket:
                await websocket.send("before")
                assert await websocket.recv() == "before"
                await asyncio.sleep(0.6)
                await websocket.send("after")
                assert await websocket.recv() == "after"

        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(hot_http())
                tasks.create_task(active_response())
                tasks.create_task(upgraded_socket())
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=3)
