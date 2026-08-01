from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter
from shared.events import Event, EventLevel
from shared.worker_events import GATEWAY_REQUEST_EVENT_ACTION
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_ASGI_HEADERS = TypeAdapter(list[tuple[bytes, bytes]])
_ASGI_CLIENT: TypeAdapter[tuple[str, int] | None] = TypeAdapter(tuple[str, int] | None)


class GatewayEventSink(Protocol):
    def emit(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        message: str,
        level: EventLevel = EventLevel.Info,
        data: dict[str, JsonValue] | None = None,
        workspace_id: str | None = None,
    ) -> Event: ...


WorkspaceResolver = Callable[[Scope], str]


class GatewayMetricsSink(Protocol):
    def increment(
        self,
        name: str,
        amount: float = 1,
        *,
        labels: dict[str, str] | None = None,
    ) -> object: ...

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> object: ...


@dataclass(slots=True)
class GatewayRequestEventMiddleware:
    """Meter every request; persist an event only for failed (5xx) responses.

    Per-request info rows are pure write amplification against the events
    audit table, so only server errors leave a durable event trail. Volume
    and latency go to the metrics sink, labelled by route template — never
    the raw path, whose cardinality is unbounded.
    """

    app: ASGIApp
    event_sink: GatewayEventSink
    workspace_resolver: WorkspaceResolver | None = None
    metrics_sink: GatewayMetricsSink | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or status_code)
            await send(message)

        started = time.monotonic()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self._emit_request_metrics(scope, status_code, time.monotonic() - started)
            self._emit_request_event(scope, status_code)

    def _emit_request_metrics(
        self,
        scope: Scope,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        if self.metrics_sink is None:
            return
        try:
            route = scope.get("route")
            template = str(getattr(route, "path", "") or "unmatched")
            method = str(scope.get("method") or "GET")
            self.metrics_sink.increment(
                "http_requests_total",
                labels={
                    "method": method,
                    "route": template,
                    "status_class": f"{status_code // 100}xx",
                },
            )
            self.metrics_sink.observe_histogram(
                "http_request_duration_seconds",
                duration_seconds,
                labels={"method": method, "route": template},
            )
        except Exception:
            return

    def _emit_request_event(self, scope: Scope, status_code: int) -> None:
        if status_code < 500:
            return
        try:
            path = str(scope.get("path") or "/")
            method = str(scope.get("method") or "GET")
            request_id = _header(scope, "x-request-id") or uuid4().hex
            workspace_id = self.workspace_resolver(scope) if self.workspace_resolver else ""
            self.event_sink.emit(
                GATEWAY_REQUEST_EVENT_ACTION,
                resource_type="gateway-request",
                resource_id=request_id,
                message=f"{method} {path} -> {status_code}",
                level=EventLevel.Error,
                data={
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "host": _header(scope, "host"),
                    "user_agent": _header(scope, "user-agent"),
                    "content_type": _header(scope, "content-type"),
                    "accept": _header(scope, "accept"),
                    "request_id": request_id,
                    "client": _client(scope),
                },
                workspace_id=workspace_id or None,
            )
        except Exception:
            return


def authorization_header_from_scope(scope: Scope) -> str | None:
    return _header(scope, "authorization") or None


def _header(scope: Scope, name: str) -> str:
    expected = name.lower().encode("latin-1")
    for raw_name, raw_value in _ASGI_HEADERS.validate_python(scope.get("headers", [])):
        if raw_name.lower() == expected:
            return raw_value.decode("latin-1")
    return ""


def _client(scope: Scope) -> str:
    client = _ASGI_CLIENT.validate_python(scope.get("client"))
    return client[0] if client is not None else ""
