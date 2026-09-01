from __future__ import annotations

import hmac
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Protocol
from urllib.parse import urlparse

from gateway.http import (
    AgentRoute,
    UpdateAgentRouteStatusRequest,
    UpdateAgentRouteStatusResponse,
)
from networking.routing import parse_backend_route_preface
from networking.wireguard import WIREGUARD_AGENT_ROUTE_PROXY_PORT
from pydantic import TypeAdapter, field_validator
from shared.app_identity import AGENT_NAME
from shared.contracts import ContractModel
from shared.http.errors import HttpApiError
from shared.routing import BackendRouteState

from agent_app.telemetry import (
    AgentTelemetryBuffer,
    AgentTelemetryEventType,
    AgentTelemetrySource,
    AgentTelemetryStream,
)

DEFAULT_ROUTE_PROXY_PREFACE_TIMEOUT_SECONDS = 10.0
DEFAULT_ROUTE_PROXY_LOCAL_DIAL_TIMEOUT_SECONDS = 2.0
DEFAULT_ROUTE_PROXY_READY_DIAL_TIMEOUT_SECONDS = 0.25
DEFAULT_ROUTE_PROXY_MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_ROUTE_PROXY_PORT = WIREGUARD_AGENT_ROUTE_PROXY_PORT
ROUTE_PROXY_READ_BUFFER_BYTES = 64 * 1024
ROUTE_PROXY_MAX_PREFACE_BYTES = 4096
_IPV4_SOCKET_ADDRESS_ADAPTER = TypeAdapter(tuple[str, int])


class RouteProxyEventAction(StrEnum):
    Ready = "route.ready"
    Degraded = "route.degraded"
    ProxyFailure = "route.proxy-failure"


class AgentRouteStatusClient(Protocol):
    def update_agent_route_status(
        self,
        request: UpdateAgentRouteStatusRequest,
    ) -> UpdateAgentRouteStatusResponse: ...


class AgentRouteProxyConfig(ContractModel):
    bind_host: str = "127.0.0.1"
    bind_port: int = DEFAULT_ROUTE_PROXY_PORT
    advertise_host: str = ""
    preface_timeout_seconds: float = DEFAULT_ROUTE_PROXY_PREFACE_TIMEOUT_SECONDS
    local_dial_timeout_seconds: float = DEFAULT_ROUTE_PROXY_LOCAL_DIAL_TIMEOUT_SECONDS
    ready_dial_timeout_seconds: float = DEFAULT_ROUTE_PROXY_READY_DIAL_TIMEOUT_SECONDS
    max_consecutive_failures: int = DEFAULT_ROUTE_PROXY_MAX_CONSECUTIVE_FAILURES

    @field_validator(
        "preface_timeout_seconds",
        "local_dial_timeout_seconds",
        "ready_dial_timeout_seconds",
    )
    @classmethod
    def timeouts_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            msg = "agent route proxy timeouts must be positive"
            raise ValueError(msg)
        return value

    @field_validator("bind_port")
    @classmethod
    def bind_port_must_be_valid(cls, value: int) -> int:
        if not 0 <= value <= 65535:
            msg = "agent route proxy bind port must be between 0 and 65535"
            raise ValueError(msg)
        return value

    @field_validator("max_consecutive_failures")
    @classmethod
    def max_consecutive_failures_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "agent route proxy failure threshold must be positive"
            raise ValueError(msg)
        return value


@dataclass(slots=True)
class RouteProxyPreface:
    route_id: str
    credential: str
    remainder: bytes = b""


@dataclass(frozen=True, slots=True)
class RouteProxyTarget:
    local_target: str
    credential: str = field(repr=False)


@dataclass(slots=True)
class AgentRouteProxyService:
    config: AgentRouteProxyConfig
    client: AgentRouteStatusClient
    agent_token: str
    telemetry: AgentTelemetryBuffer | None = None
    _routes: dict[str, RouteProxyTarget] = field(default_factory=dict, init=False)
    _failure_counts: dict[str, int] = field(default_factory=dict, init=False)
    _listener: socket.socket | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _closed: threading.Event = field(default_factory=threading.Event, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def proxy_target(self) -> str:
        listener = self._listener
        if listener is None:
            return ""
        host = self.config.advertise_host or _advertise_host(self.config.bind_host)
        _bound_host, port = _IPV4_SOCKET_ADDRESS_ADAPTER.validate_python(listener.getsockname())
        return _join_host_port(host, port)

    def start(self) -> AgentRouteProxyService:
        if self._listener is not None:
            return self
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.config.bind_host, self.config.bind_port))
        listener.listen()
        listener.settimeout(0.25)
        self._listener = listener
        self._thread = threading.Thread(
            target=self._accept_loop,
            name=f"{AGENT_NAME}-route-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        self._closed.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def __enter__(self) -> AgentRouteProxyService:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc_value, traceback
        self.close()

    def reconcile_routes(self, routes: list[AgentRoute]) -> int:
        if self._listener is None:
            self.start()
        seen: set[str] = set()
        for route in routes:
            if not route.route_id or not route.local_target or not route.proxy_auth_token:
                continue
            seen.add(route.route_id)
            self.set_route(route.route_id, route.local_target, route.proxy_auth_token)
            if route.state is BackendRouteState.Ready and route.proxy_target == self.proxy_target:
                continue
            ok, latency_ms = local_target_ready(
                route.local_target,
                timeout_seconds=self.config.ready_dial_timeout_seconds,
            )
            if not ok:
                continue
            self._update_route_ready(route.route_id, route.local_target, latency_ms)
        self.delete_routes_not_in(seen)
        return self.route_count()

    def set_route(self, route_id: str, local_target: str, credential: str) -> None:
        if not credential:
            raise ValueError("route proxy credential is required")
        with self._lock:
            previous = self._routes.get(route_id)
            target = RouteProxyTarget(local_target=local_target, credential=credential)
            self._routes[route_id] = target
            if previous and previous != target:
                self._failure_counts.pop(route_id, None)

    def delete_routes_not_in(self, route_ids: set[str]) -> None:
        with self._lock:
            for route_id in list(self._routes):
                if route_id in route_ids:
                    continue
                self._routes.pop(route_id, None)
                self._failure_counts.pop(route_id, None)

    def route_count(self) -> int:
        with self._lock:
            return len(self._routes)

    def authorize_route(self, route_id: str, credential: str) -> str:
        with self._lock:
            target = self._routes.get(route_id)
            if target is None or not hmac.compare_digest(target.credential, credential):
                return ""
            return target.local_target

    def handle_connection(self, connection: socket.socket) -> None:
        with connection:
            line = self._read_preface_line(connection)
            if line is None:
                return
            text, remainder = line
            preface = self._parse_route_preface(text, remainder)
            if preface is None:
                return
            local_target = self.authorize_route(preface.route_id, preface.credential)
            if not local_target:
                return
            started = time.monotonic()
            try:
                local = dial_local_target(
                    local_target,
                    timeout_seconds=self.config.local_dial_timeout_seconds,
                )
            except OSError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                self.record_route_failure(preface.route_id, local_target, latency_ms, exc)
                return
            with local:
                if preface.remainder:
                    local.sendall(preface.remainder)
                _copy_both(connection, local)
            self.reset_route_failures(preface.route_id)

    def record_route_failure(
        self,
        route_id: str,
        local_target: str,
        dial_latency_ms: int,
        cause: BaseException,
    ) -> None:
        with self._lock:
            target = self._routes.get(route_id)
            if target is None or target.local_target != local_target:
                return
            count = self._failure_counts.get(route_id, 0) + 1
            self._failure_counts[route_id] = count
        self._enqueue_route_event(
            RouteProxyEventAction.ProxyFailure,
            status=BackendRouteState.Opening.value,
            message=str(cause),
            attrs={
                "route_id": route_id,
                "local_target": local_target,
                "failure_count": str(count),
            },
        )
        if count < self.config.max_consecutive_failures:
            return
        self.mark_route_degraded(route_id, local_target, dial_latency_ms, cause)

    def reset_route_failures(self, route_id: str) -> None:
        with self._lock:
            self._failure_counts.pop(route_id, None)

    def mark_route_degraded(
        self,
        route_id: str,
        local_target: str,
        dial_latency_ms: int,
        cause: BaseException,
    ) -> None:
        with self._lock:
            target = self._routes.get(route_id)
            if target is None or target.local_target != local_target:
                return
            self._routes.pop(route_id, None)
            self._failure_counts.pop(route_id, None)
        attrs = {
            "local_target": local_target,
            "proxy_target": self.proxy_target,
            "reason": str(cause),
        }
        if dial_latency_ms > 0:
            attrs["local_dial_ms"] = str(dial_latency_ms)
        self._report_route_status(
            UpdateAgentRouteStatusRequest(
                agent_token=self.agent_token,
                route_id=route_id,
                state=BackendRouteState.Degraded,
                proxy_target=self.proxy_target,
                error=str(cause),
                attrs=attrs,
            )
        )
        self._enqueue_route_event(
            RouteProxyEventAction.Degraded,
            status=BackendRouteState.Degraded.value,
            message=str(cause),
            attrs={"route_id": route_id, **attrs},
        )

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                connection = listener.accept()[0]
            except TimeoutError:
                continue
            except OSError:
                if self._closed.is_set():
                    return
                continue
            threading.Thread(
                target=self._handle_connection_safely,
                args=(connection,),
                name=f"{AGENT_NAME}-route-proxy-connection",
                daemon=True,
            ).start()

    def _handle_connection_safely(self, connection: socket.socket) -> None:
        try:
            self.handle_connection(connection)
        except Exception as exc:
            if self.telemetry is None:
                return
            self.telemetry.enqueue_log(
                f"route proxy connection failed: {exc}",
                source=AgentTelemetrySource.RouteProxy,
                stream=AgentTelemetryStream.Stderr,
            )

    def _read_preface_line(self, connection: socket.socket) -> tuple[str, bytes] | None:
        connection.settimeout(self.config.preface_timeout_seconds)
        buffer = b""
        try:
            while b"\n" not in buffer and len(buffer) <= ROUTE_PROXY_MAX_PREFACE_BYTES:
                chunk = connection.recv(min(1024, ROUTE_PROXY_MAX_PREFACE_BYTES - len(buffer)))
                if not chunk:
                    return None
                buffer += chunk
        except OSError:
            return None
        finally:
            connection.settimeout(None)
        line, separator, remainder = buffer.partition(b"\n")
        if not separator:
            return None
        return (line.decode("utf-8", errors="replace").rstrip("\r"), remainder)

    def _parse_route_preface(self, text: str, remainder: bytes) -> RouteProxyPreface | None:
        parsed = parse_backend_route_preface(text)
        if parsed is None:
            return None
        route_id, credential = parsed
        return RouteProxyPreface(
            route_id=route_id,
            credential=credential,
            remainder=remainder,
        )

    def _report_route_status(self, request: UpdateAgentRouteStatusRequest) -> bool:
        """Report one route's status, surviving a refusal.

        Reporting is how the agent converges on the control plane's view, so a
        refusal is about that one route and never about the daemon. Letting it
        propagate exited the process, and every restart replayed the same report
        against the same route: one route the control plane would not accept
        stopped the agent from serving any of them.
        """
        try:
            self.client.update_agent_route_status(request)
        except HttpApiError as exc:
            with self._lock:
                self._routes.pop(request.route_id, None)
                self._failure_counts.pop(request.route_id, None)
            self._enqueue_route_event(
                RouteProxyEventAction.Degraded,
                status=BackendRouteState.Degraded.value,
                message=str(exc),
                attrs={"route_id": request.route_id, "reason": "status update refused"},
            )
            return False
        return True

    def _update_route_ready(self, route_id: str, local_target: str, latency_ms: int) -> None:
        attrs = {
            "local_target": local_target,
            "proxy_target": self.proxy_target,
            "local_dial_ms": str(latency_ms),
        }
        if not self._report_route_status(
            UpdateAgentRouteStatusRequest(
                agent_token=self.agent_token,
                route_id=route_id,
                state=BackendRouteState.Ready,
                proxy_target=self.proxy_target,
                attrs=attrs,
            )
        ):
            return
        self._enqueue_route_event(
            RouteProxyEventAction.Ready,
            status=BackendRouteState.Ready.value,
            attrs={"route_id": route_id, **attrs},
        )

    def _enqueue_route_event(
        self,
        action: RouteProxyEventAction,
        *,
        status: str,
        message: str = "",
        attrs: dict[str, str] | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        self.telemetry.enqueue_event(
            event_type=AgentTelemetryEventType.Route,
            action=action.value,
            status=status,
            message=message,
            attrs=attrs or {},
        )


def local_target_ready(local_target: str, *, timeout_seconds: float = 0.25) -> tuple[bool, int]:
    started = time.monotonic()
    try:
        connection = dial_local_target(local_target, timeout_seconds=timeout_seconds)
    except OSError:
        return (False, 0)
    with connection:
        return (True, int((time.monotonic() - started) * 1000))


def dial_local_target(local_target: str, *, timeout_seconds: float) -> socket.socket:
    host, port = target_host_port(local_target)
    if not host or port <= 0:
        msg = f"invalid local target: {local_target}"
        raise OSError(msg)
    try:
        return _connect_within(host, port, timeout_seconds)
    except OSError as first_error:
        fallback_host = "127.0.0.1"
        if host in {fallback_host, "localhost"}:
            raise
        try:
            return _connect_within(fallback_host, port, timeout_seconds)
        except OSError as fallback_error:
            msg = (
                f"dial {local_target} failed: {first_error}; "
                f"loopback fallback {fallback_host}:{port} failed: {fallback_error}"
            )
            raise OSError(msg) from fallback_error


def _connect_within(host: str, port: int, timeout_seconds: float) -> socket.socket:
    """Bound how long the dial may take, but not how long the connection may live.

    ``create_connection`` leaves its timeout on the socket it returns, so it would
    apply to every later ``recv``. An idle terminal or a stream between messages may
    stay quiet past the dial budget. That must not tear the tunnel down.
    """
    connection = socket.create_connection((host, port), timeout=timeout_seconds)
    connection.settimeout(None)
    return connection


def target_host_port(target: str) -> tuple[str, int]:
    parsed = urlparse(target)
    if parsed.hostname and parsed.port:
        return (parsed.hostname, parsed.port)
    host, separator, port_text = target.rpartition(":")
    if not separator:
        return ("", 0)
    try:
        port = int(port_text)
    except ValueError:
        return ("", 0)
    return (host.strip("[]"), port)


def _copy_both(left: socket.socket, right: socket.socket) -> None:
    threads = [
        threading.Thread(target=_copy_socket, args=(left, right), daemon=True),
        threading.Thread(target=_copy_socket, args=(right, left), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _copy_socket(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(ROUTE_PROXY_READ_BUFFER_BYTES)
            if not data:
                break
            target.sendall(data)
    except OSError:
        return
    finally:
        _shutdown_write(target)


def _shutdown_write(connection: socket.socket) -> None:
    with suppress(OSError):
        connection.shutdown(socket.SHUT_WR)


def _advertise_host(bind_host: str) -> str:
    if bind_host in {"", "0.0.0.0", "::"}:
        return socket.gethostname()
    return bind_host


def _join_host_port(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"
