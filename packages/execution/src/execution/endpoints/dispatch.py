from __future__ import annotations

import http.client
import socket
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from foundation.http import (
    forwarded_request_headers,
    forwarded_request_path,
    grouped_response_headers,
)
from networking.dialer import (
    BackendRouteDialer,
    BackendRouteDialerConfig,
    BackendRouteResolver,
    BackendRouteUnavailable,
    TailnetPeerResolver,
    TailnetPeerWaiter,
)
from networking.routing import build_backend_route_dial_plan
from pydantic import Field
from shared.container_requests import CONTAINER_HEALTH_PATH, CONTAINER_INNER_PORT
from shared.contracts import ContractModel
from shared.deployment_records import DEFAULT_MAX_PENDING_TASKS
from shared.http.endpoints import EndpointForwardRequest, EndpointForwardResponse
from shared.scheduling import SchedulerContainerStatus
from shared.timestamps import utc_now
from shared.urls import parse_container_address

from execution.containers.readiness import (
    AlwaysReadyContainers,
    ContainerHealthCheck,
    ContainerReadiness,
)

DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS = 175.0
DEFAULT_ENDPOINT_QUEUE_TIMEOUT_SECONDS = 600.0
DEFAULT_ENDPOINT_REQUEST_BUFFER_SIZE = DEFAULT_MAX_PENDING_TASKS
DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY = 1
ENDPOINT_DISPATCH_TASK_KEY = "__endpoint_dispatch"


class EndpointDispatchUnavailable(RuntimeError):
    pass


class EndpointDispatchError(RuntimeError):
    pass


class EndpointBackendUnreachable(RuntimeError):
    """No request byte reached the backend, so another target may still be tried.

    What this carries is not which error occurred but how far the exchange got.
    Only the window before the request is written can raise it, which is why it is
    raised where the connection is established and nowhere above: once the body has
    left this process a second attempt would replay it, and whether the application
    already acted on it is no longer knowable from here.

    A status the application itself returned is a response, not a failure, and comes
    back through the ordinary return. Retry policy for those belongs to the caller
    that owns it, never to the transport.
    """


class EndpointDispatchStatus(StrEnum):
    Queued = "queued"
    WaitingCapacity = "waiting-capacity"
    Inflight = "inflight"
    Complete = "complete"
    Failed = "failed"
    Timeout = "timeout"
    Cancelled = "cancelled"


class EndpointBackendProtocol(StrEnum):
    Http = "http"
    WebSocket = "ws"


class EndpointDispatchRecord(ContractModel):
    task_id: str
    stub_id: str
    workspace_id: str
    method: str
    path: str
    status: EndpointDispatchStatus = EndpointDispatchStatus.Queued
    container_id: str | None = None
    wait_timeout_seconds: float = DEFAULT_ENDPOINT_QUEUE_TIMEOUT_SECONDS
    max_pending_requests: int = DEFAULT_ENDPOINT_REQUEST_BUFFER_SIZE
    max_inflight_per_container: int = DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY
    attempts: int = 0
    enqueued_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    def transition(
        self,
        status: EndpointDispatchStatus,
        *,
        container_id: str | None = None,
        error: str | None = None,
        heartbeat: bool = True,
    ) -> EndpointDispatchRecord:
        now = utc_now()
        self.status = status
        if container_id is not None:
            self.container_id = container_id
        if status is EndpointDispatchStatus.Inflight and self.started_at is None:
            self.started_at = now
        if status in TERMINAL_ENDPOINT_DISPATCH_STATUSES:
            self.finished_at = now
        if heartbeat:
            self.heartbeat_at = now
        if error is not None:
            self.error = error
        return self


TERMINAL_ENDPOINT_DISPATCH_STATUSES: frozenset[EndpointDispatchStatus] = frozenset(
    {
        EndpointDispatchStatus.Complete,
        EndpointDispatchStatus.Failed,
        EndpointDispatchStatus.Timeout,
        EndpointDispatchStatus.Cancelled,
    }
)

ACTIVE_ENDPOINT_DISPATCH_STATUSES: frozenset[EndpointDispatchStatus] = frozenset(
    {
        EndpointDispatchStatus.Queued,
        EndpointDispatchStatus.WaitingCapacity,
        EndpointDispatchStatus.Inflight,
    }
)


class EndpointBackendRoute(Protocol):
    @property
    def route_id(self) -> str: ...

    @property
    def port(self) -> int: ...


class EndpointContainerState(Protocol):
    @property
    def container_id(self) -> str: ...

    @property
    def status(self) -> SchedulerContainerStatus: ...

    @property
    def scheduled_at(self) -> datetime: ...

    @property
    def started_at(self) -> datetime | None: ...

    @property
    def failure_reason(self) -> str: ...


class EndpointContainerAddress(Protocol):
    @property
    def address(self) -> str: ...

    @property
    def route(self) -> EndpointBackendRoute | None: ...


class EndpointContainerAddressMap(Protocol):
    @property
    def address_map(self) -> Mapping[int, str]: ...

    @property
    def routes(self) -> Iterable[EndpointBackendRoute]: ...


class EndpointContainerRepository(Protocol):
    def list_by_stub(self, stub_id: str) -> Sequence[EndpointContainerState]: ...

    def get_container_address(self, container_id: str) -> EndpointContainerAddress | None: ...

    def get_container_address_map(self, container_id: str) -> EndpointContainerAddressMap: ...


class EndpointRequestDispatcher(Protocol):
    def forward(
        self,
        *,
        stub_id: str,
        request: EndpointForwardRequest,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
    ) -> EndpointForwardResponse: ...

    def forward_target(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> EndpointForwardResponse: ...

    def select_target(
        self,
        stub_id: str,
        *,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
    ) -> EndpointDispatchTarget | None: ...

    def container_states(self, stub_id: str) -> Sequence[EndpointContainerState]: ...

    def open_backend_socket(self, target: EndpointDispatchTarget) -> socket.socket | None: ...

    def open_http_stream(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> EndpointResponseStream: ...


@dataclass(frozen=True, slots=True)
class EndpointDispatchTarget:
    container_id: str
    address: str
    route: EndpointBackendRoute | None = None


class EndpointResponseStream(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> dict[str, list[str]]: ...

    def iter_chunks(self, chunk_size: int = 64 * 1024) -> Iterable[bytes]: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class EndpointHttpResponseStream:
    connection: http.client.HTTPConnection
    response: http.client.HTTPResponse

    @property
    def status_code(self) -> int:
        return self.response.status

    @property
    def headers(self) -> dict[str, list[str]]:
        return grouped_response_headers(self.response.getheaders())

    def iter_chunks(self, chunk_size: int = 64 * 1024) -> Iterable[bytes]:
        while chunk := self.response.read1(chunk_size):
            yield chunk

    def close(self) -> None:
        self.connection.close()


@dataclass(slots=True)
class EndpointInstanceDispatcher:
    containers: EndpointContainerRepository
    route_resolver: BackendRouteResolver | None = None
    route_dialer_config: BackendRouteDialerConfig = field(default_factory=BackendRouteDialerConfig)
    tailnet_peer_waiter: TailnetPeerWaiter | None = None
    tailnet_peer_resolver: TailnetPeerResolver | None = None
    endpoint_port: int = CONTAINER_INNER_PORT
    http_client: EndpointHttpClient | None = None
    readiness: ContainerReadiness = field(default_factory=AlwaysReadyContainers)

    def forward(
        self,
        *,
        stub_id: str,
        request: EndpointForwardRequest,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
    ) -> EndpointForwardResponse:
        targets = self.targets(
            stub_id,
            container_loads=container_loads,
            max_inflight_per_container=max_inflight_per_container,
        )
        if not targets:
            msg = f"no running endpoint containers for stub {stub_id}"
            raise EndpointDispatchUnavailable(msg)

        errors: list[str] = []
        client = self.http_client or EndpointHttpClient(
            route_resolver=self.route_resolver,
            route_dialer_config=self.route_dialer_config,
            tailnet_peer_waiter=self.tailnet_peer_waiter,
            tailnet_peer_resolver=self.tailnet_peer_resolver,
        )
        for target in targets:
            try:
                return client.forward(target, request, timeout_seconds=timeout_seconds)
            except Exception as exc:
                errors.append(f"{target.container_id}: {type(exc).__name__}: {exc}")
        msg = "all endpoint containers failed"
        if errors:
            msg = f"{msg}: {'; '.join(errors)}"
        raise EndpointDispatchError(msg)

    def forward_target(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> EndpointForwardResponse:
        client = self.http_client or EndpointHttpClient(
            route_resolver=self.route_resolver,
            route_dialer_config=self.route_dialer_config,
            tailnet_peer_waiter=self.tailnet_peer_waiter,
            tailnet_peer_resolver=self.tailnet_peer_resolver,
        )
        return client.forward(target, request, timeout_seconds=timeout_seconds)

    def select_target(
        self,
        stub_id: str,
        *,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
    ) -> EndpointDispatchTarget | None:
        targets = self.targets(
            stub_id,
            container_loads=container_loads,
            max_inflight_per_container=max_inflight_per_container,
        )
        return targets[0] if targets else None

    def container_states(self, stub_id: str) -> Sequence[EndpointContainerState]:
        return self.containers.list_by_stub(stub_id)

    def open_backend_socket(self, target: EndpointDispatchTarget) -> socket.socket | None:
        route = target.route
        if route is None or not route.route_id:
            return None
        connection = BackendRouteDialer(
            resolver=self.route_resolver,
            config=self.route_dialer_config,
            tailnet_peer_waiter=self.tailnet_peer_waiter,
            tailnet_peer_resolver=self.tailnet_peer_resolver,
        ).dial_plan(build_backend_route_dial_plan(route.route_id))
        if not isinstance(connection, socket.socket):
            connection.close()
            msg = "endpoint backend dialer returned a non-socket connection"
            raise TypeError(msg)
        return connection

    def open_http_stream(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> EndpointResponseStream:
        client = self.http_client or EndpointHttpClient(
            route_resolver=self.route_resolver,
            route_dialer_config=self.route_dialer_config,
            tailnet_peer_waiter=self.tailnet_peer_waiter,
            tailnet_peer_resolver=self.tailnet_peer_resolver,
        )
        return client.open_stream(target, request, timeout_seconds=timeout_seconds)

    def targets(
        self,
        stub_id: str,
        *,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
    ) -> list[EndpointDispatchTarget]:
        states = [
            state
            for state in self.containers.list_by_stub(stub_id)
            if state.status is SchedulerContainerStatus.Running
        ]
        loads = dict(container_loads or {})
        states = [
            state
            for state in states
            if loads.get(state.container_id, 0) < max(max_inflight_per_container, 1)
        ]
        states.sort(key=lambda state: _state_sort_key(state, loads))
        targets: list[EndpointDispatchTarget] = []
        for state in states:
            target = self._target_for_state(state)
            if target is not None:
                targets.append(target)
        return self._ready_targets(targets, stub_id)

    def _ready_targets(
        self,
        targets: list[EndpointDispatchTarget],
        stub_id: str,
    ) -> list[EndpointDispatchTarget]:
        """Drop containers whose runner is not answering yet.

        A container reaching `Running` has started, not bound its port, and the
        sort above would otherwise hand a caller the least loaded backend
        precisely because nothing has reached it.
        """

        if not targets:
            return targets
        health_check = ContainerHealthCheck(
            path=CONTAINER_HEALTH_PATH,
            port=self.endpoint_port,
        )

        def ready(target: EndpointDispatchTarget) -> bool:
            route = target.route
            return self.readiness.is_ready(
                container_id=target.container_id,
                stub_id=stub_id,
                address=target.address,
                route_id=route.route_id if route is not None else "",
                port=self.endpoint_port,
                health_check=health_check,
            )

        if len(targets) == 1:
            return targets if ready(targets[0]) else []
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            verdicts = list(pool.map(ready, targets))
        return [target for target, is_ready in zip(targets, verdicts, strict=True) if is_ready]

    def _target_for_state(
        self,
        state: EndpointContainerState,
    ) -> EndpointDispatchTarget | None:
        primary = self.containers.get_container_address(state.container_id)
        if primary is not None and primary.address:
            return EndpointDispatchTarget(
                container_id=state.container_id,
                address=primary.address,
                route=primary.route,
            )

        address_map = self.containers.get_container_address_map(state.container_id)
        address = address_map.address_map.get(self.endpoint_port, "")
        route = next((item for item in address_map.routes if item.port == self.endpoint_port), None)
        if not address:
            return None
        return EndpointDispatchTarget(
            container_id=state.container_id,
            address=address,
            route=route,
        )


@dataclass(slots=True)
class EndpointHttpClient:
    route_resolver: BackendRouteResolver | None = None
    route_dialer_config: BackendRouteDialerConfig = field(default_factory=BackendRouteDialerConfig)
    tailnet_peer_waiter: TailnetPeerWaiter | None = None
    tailnet_peer_resolver: TailnetPeerResolver | None = None

    def forward(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> EndpointForwardResponse:
        stream = self.open_stream(target, request, timeout_seconds=timeout_seconds)
        try:
            body = b"".join(stream.iter_chunks())
            return EndpointForwardResponse(
                status_code=stream.status_code,
                headers=stream.headers,
                body=body,
            )
        finally:
            stream.close()

    def open_stream(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> EndpointHttpResponseStream:
        connection = self._connection(target, timeout_seconds)
        headers = forwarded_request_headers(request.headers)
        try:
            connection.request(
                request.method,
                forwarded_request_path(request.path, request.query_params),
                body=request.body,
                headers=headers,
            )
            response = connection.getresponse()
        except Exception:
            connection.close()
            raise
        return EndpointHttpResponseStream(connection=connection, response=response)

    def _connection(
        self,
        target: EndpointDispatchTarget,
        timeout_seconds: float,
    ) -> http.client.HTTPConnection:
        try:
            connection = self._unconnected(target, timeout_seconds)
        except (OSError, BackendRouteUnavailable) as exc:
            raise EndpointBackendUnreachable(str(exc)) from exc
        # Connect here rather than leaving it to the first write: the handshake is
        # the last moment at which nothing has been sent, so it is the only place a
        # failure can still be told apart from one that may have been acted on.
        try:
            connection.connect()
        except OSError as exc:
            connection.close()
            raise EndpointBackendUnreachable(str(exc)) from exc
        return connection

    def _unconnected(
        self,
        target: EndpointDispatchTarget,
        timeout_seconds: float,
    ) -> http.client.HTTPConnection:
        timeout = timeout_seconds or self.route_dialer_config.timeout_seconds
        if target.route is not None and target.route.route_id:
            connection = BackendRouteDialer(
                resolver=self.route_resolver,
                config=self.route_dialer_config,
                tailnet_peer_waiter=self.tailnet_peer_waiter,
                tailnet_peer_resolver=self.tailnet_peer_resolver,
            ).dial_plan(build_backend_route_dial_plan(target.route.route_id))
            if not isinstance(connection, socket.socket):
                connection.close()
                msg = "endpoint HTTP dialer returned a non-socket connection"
                raise TypeError(msg)
            return _RouteHttpConnection(connection, timeout=timeout)

        parsed = parse_container_address(target.address, resource="endpoint")
        if parsed.scheme == "https":
            return http.client.HTTPSConnection(parsed.hostname or "", parsed.port, timeout=timeout)
        return http.client.HTTPConnection(parsed.hostname or "", parsed.port, timeout=timeout)


class _RouteHttpConnection(http.client.HTTPConnection):
    def __init__(self, route_socket: socket.socket, *, timeout: float) -> None:
        super().__init__("backend.route", timeout=timeout)
        self._route_socket = route_socket

    def connect(self) -> None:
        self.sock = self._route_socket


def _state_sort_key(
    state: EndpointContainerState,
    container_loads: Mapping[str, int],
) -> tuple[int, datetime, str]:
    return (
        container_loads.get(state.container_id, 0),
        state.started_at or state.scheduled_at,
        state.container_id,
    )


def endpoint_backend_url(
    target: EndpointDispatchTarget,
    request: EndpointForwardRequest,
    *,
    protocol: EndpointBackendProtocol = EndpointBackendProtocol.Http,
) -> str:
    parsed = parse_container_address(target.address, resource="endpoint")
    scheme = parsed.scheme
    if protocol is EndpointBackendProtocol.WebSocket:
        scheme = "wss" if parsed.scheme == "https" else "ws"
    host = "backend.route" if target.route is not None and target.route.route_id else parsed.netloc
    return f"{scheme}://{host}{forwarded_request_path(request.path, request.query_params)}"
