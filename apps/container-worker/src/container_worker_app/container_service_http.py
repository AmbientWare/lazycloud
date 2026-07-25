from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import JsonValue, TypeAdapter
from worker.container_client.models import ContainerServiceMethod, ContainerServicePayload
from worker.container_client.wire import (
    CONTAINER_SERVICE_HTTP_PREFIX,
    decode_container_service_wire_value,
    encode_container_service_wire_value,
)

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class ContainerServiceRequestHandler(Protocol):
    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContainerServicePayload,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerServicePayload: ...

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContainerServicePayload,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterable[ContainerServicePayload]: ...


@dataclass(slots=True)
class ContainerServiceHttpServer:
    server: uvicorn.Server
    thread: threading.Thread

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=timeout_seconds)


def create_container_service_app(
    transport: ContainerServiceRequestHandler,
    *,
    token: str = "",
) -> FastAPI:
    app = FastAPI(title="Worker Container Service", docs_url=None, redoc_url=None)

    @app.post(f"{CONTAINER_SERVICE_HTTP_PREFIX}/{{method_name}}")
    async def unary(
        method_name: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authorize(token, authorization)
        method = _method(method_name)
        payload = decode_container_service_wire_value(
            _JSON_VALUE.validate_json(await request.body())
        )
        result = transport.unary(method, payload)
        return JSONResponse(content=encode_container_service_wire_value(result))

    @app.post(f"{CONTAINER_SERVICE_HTTP_PREFIX}/{{method_name}}/stream")
    async def stream(
        method_name: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        _authorize(token, authorization)
        method = _method(method_name)
        payload = decode_container_service_wire_value(
            _JSON_VALUE.validate_json(await request.body())
        )
        return StreamingResponse(
            _stream_body(transport.stream(method, payload)),
            media_type="application/x-ndjson",
        )

    return app


def start_container_service_http_server(
    transport: ContainerServiceRequestHandler,
    *,
    host: str,
    port: int,
    token: str = "",
    log_level: str = "warning",
    startup_timeout_seconds: float = 5.0,
) -> ContainerServiceHttpServer:
    if port <= 0:
        msg = "container service port must be greater than zero"
        raise ValueError(msg)
    config = uvicorn.Config(
        create_container_service_app(transport, token=token),
        host=host,
        port=port,
        log_level=log_level,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="container-service-http", daemon=True)
    thread.start()
    _wait_for_startup(server, thread, timeout_seconds=startup_timeout_seconds)
    return ContainerServiceHttpServer(server=server, thread=thread)


def _authorize(token: str, authorization: str | None) -> None:
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="container service authorization failed")


def _method(method_name: str) -> ContainerServiceMethod:
    try:
        return ContainerServiceMethod(method_name)
    except ValueError as exc:
        detail = f"unknown container service method: {method_name}"
        raise HTTPException(status_code=404, detail=detail) from exc


def _stream_body(items: Iterable[ContainerServicePayload]) -> Iterable[bytes]:
    for item in items:
        payload = encode_container_service_wire_value(item)
        yield (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _wait_for_startup(
    server: uvicorn.Server,
    thread: threading.Thread,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while not server.started and thread.is_alive():
        if time.monotonic() >= deadline:
            msg = "container service HTTP server did not start before deadline"
            raise RuntimeError(msg)
        time.sleep(0.05)
