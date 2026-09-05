from __future__ import annotations

import asyncio
import socket
import sys
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from foundation.http import (
    forwarded_request_headers,
    forwarded_request_path,
)
from networking.async_http import (
    AsyncBackendConnectError,
    AsyncBackendHttpClient,
)
from networking.dialer import (
    BackendRouteDialer,
    BackendRouteDialerConfig,
    BackendRouteResolver,
)
from networking.routing import build_backend_route_dial_plan
from pydantic import Field
from shared.container_requests import CONTAINER_HEALTH_PATH, CONTAINER_INNER_PORT
from shared.contracts import ContractModel
from shared.deployment_records import DEFAULT_MAX_PENDING_TASKS
from shared.http.endpoints import EndpointForwardRequest
from shared.scheduling import SchedulerContainerStatus
from shared.timestamps import utc_now
from shared.urls import parse_container_address

from execution.containers.readiness import AsyncContainerReadiness

DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS = 175.0
DEFAULT_ENDPOINT_QUEUE_TIMEOUT_SECONDS = 600.0
DEFAULT_ENDPOINT_REQUEST_BUFFER_SIZE = DEFAULT_MAX_PENDING_TASKS
DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY = 1
# For a caller that runs no handler, so a container already serving its
# limit can still answer. Larger than any load the dispatcher records.
UNLIMITED_ENDPOINT_CONTAINER_CONCURRENCY = sys.maxsize


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
    expires_at: datetime
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
            self.expires_at = now + timedelta(seconds=max(self.wait_timeout_seconds, 1.0))
        if error is not None or status in TERMINAL_ENDPOINT_DISPATCH_STATUSES:
            self.error = error
        return self

    def requeue(self) -> EndpointDispatchRecord:
        now = utc_now()
        self.status = EndpointDispatchStatus.Queued
        self.container_id = None
        self.enqueued_at = now
        self.started_at = None
        self.heartbeat_at = now
        self.expires_at = now + timedelta(seconds=max(self.wait_timeout_seconds, 1.0))
        self.finished_at = None
        self.error = None
        return self


TERMINAL_ENDPOINT_DISPATCH_STATUSES: frozenset[EndpointDispatchStatus] = frozenset(
    {
        EndpointDispatchStatus.Complete,
        EndpointDispatchStatus.Failed,
        EndpointDispatchStatus.Timeout,
        EndpointDispatchStatus.Cancelled,
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


class AsyncEndpointContainerRepository(Protocol):
    async def list_by_stub(self, stub_id: str) -> Sequence[EndpointContainerState]: ...

    async def get_container_address(
        self,
        container_id: str,
    ) -> EndpointContainerAddress | None: ...

    async def get_container_address_maps(
        self,
        container_ids: Sequence[str],
    ) -> Mapping[str, EndpointContainerAddressMap]: ...


@dataclass(frozen=True, slots=True)
class EndpointDispatchTarget:
    container_id: str
    address: str
    route: EndpointBackendRoute | None = None


class AsyncEndpointResponseStream(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> dict[str, list[str]]: ...

    def iter_chunks(self) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


class AsyncEndpointRequestDispatcher(Protocol):
    async def select_target(
        self,
        stub_id: str,
        *,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
        excluded_container_ids: frozenset[str] | set[str] = frozenset(),
    ) -> EndpointDispatchTarget | None: ...

    async def unprobed_target(self, stub_id: str) -> EndpointDispatchTarget | None: ...

    async def container_states(self, stub_id: str) -> Sequence[EndpointContainerState]: ...

    async def open_backend_socket(self, target: EndpointDispatchTarget) -> socket.socket | None: ...

    async def open_http_stream(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> AsyncEndpointResponseStream: ...


@dataclass(slots=True)
class AsyncEndpointInstanceDispatcher:
    containers: AsyncEndpointContainerRepository
    http_client: AsyncBackendHttpClient
    readiness_probe: AsyncContainerReadiness
    route_resolver: BackendRouteResolver | None = None
    route_dialer_config: BackendRouteDialerConfig = field(default_factory=BackendRouteDialerConfig)
    endpoint_port: int = CONTAINER_INNER_PORT

    async def ready_container_ids(self, stub_id: str, container_ids: list[str]) -> set[str]:
        wanted = set(container_ids)
        ready: set[str] = set()
        for target in await self._ordered_targets(
            stub_id,
            container_loads=None,
            max_inflight_per_container=UNLIMITED_ENDPOINT_CONTAINER_CONCURRENCY,
        ):
            if target.container_id in wanted and await self._is_ready(target, stub_id):
                ready.add(target.container_id)
        return ready

    async def select_target(
        self,
        stub_id: str,
        *,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
        excluded_container_ids: frozenset[str] | set[str] = frozenset(),
    ) -> EndpointDispatchTarget | None:
        # Probed one at a time in load order rather than all at once: the slowest
        # probe is the container that is not answering, and it would otherwise put
        # its full timeout in front of a healthy container's request.
        for target in await self._ordered_targets(
            stub_id,
            container_loads=container_loads,
            max_inflight_per_container=max_inflight_per_container,
        ):
            if target.container_id not in excluded_container_ids and await self._is_ready(
                target, stub_id
            ):
                return target
        return None

    async def unprobed_target(self, stub_id: str) -> EndpointDispatchTarget | None:
        targets = await self._ordered_targets(
            stub_id,
            container_loads=None,
            max_inflight_per_container=UNLIMITED_ENDPOINT_CONTAINER_CONCURRENCY,
        )
        return targets[0] if targets else None

    async def container_states(self, stub_id: str) -> Sequence[EndpointContainerState]:
        return await self.containers.list_by_stub(stub_id)

    async def open_backend_socket(self, target: EndpointDispatchTarget) -> socket.socket | None:
        route = target.route
        if route is None or not route.route_id:
            return None
        connection = await asyncio.to_thread(
            BackendRouteDialer(
                resolver=self.route_resolver,
                config=self.route_dialer_config,
            ).dial_plan,
            build_backend_route_dial_plan(route.route_id),
        )
        if not isinstance(connection, socket.socket):
            connection.close()
            raise TypeError("endpoint backend dialer returned a non-socket connection")
        return connection

    async def open_http_stream(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> AsyncEndpointResponseStream:
        try:
            return await self.http_client.open_stream(
                address=target.address,
                route_id=target.route.route_id if target.route is not None else "",
                method=request.method,
                path=forwarded_request_path(request.path, request.query_params),
                headers=forwarded_request_headers(request.headers),
                body=request.body,
                timeout_seconds=timeout_seconds,
                resource="endpoint",
            )
        except AsyncBackendConnectError as exc:
            raise EndpointBackendUnreachable(str(exc)) from exc

    async def _ordered_targets(
        self,
        stub_id: str,
        *,
        container_loads: Mapping[str, int] | None,
        max_inflight_per_container: int,
    ) -> list[EndpointDispatchTarget]:
        states = [
            state
            for state in await self.containers.list_by_stub(stub_id)
            if state.status is SchedulerContainerStatus.Running
        ]
        loads = dict(container_loads or {})
        states = [
            state
            for state in states
            if loads.get(state.container_id, 0) < max(max_inflight_per_container, 1)
        ]
        states.sort(key=lambda state: _state_sort_key(state, loads))
        addresses = await asyncio.gather(
            *(self.containers.get_container_address(state.container_id) for state in states)
        )
        unaddressed = [
            state.container_id
            for state, primary in zip(states, addresses, strict=True)
            if primary is None or not primary.address
        ]
        address_maps: Mapping[str, EndpointContainerAddressMap] = (
            await self.containers.get_container_address_maps(unaddressed) if unaddressed else {}
        )
        targets: list[EndpointDispatchTarget] = []
        for state, primary in zip(states, addresses, strict=True):
            if primary is not None and primary.address:
                targets.append(
                    EndpointDispatchTarget(
                        container_id=state.container_id,
                        address=primary.address,
                        route=primary.route,
                    )
                )
                continue
            address_map = address_maps[state.container_id]
            address = address_map.address_map.get(self.endpoint_port, "")
            if not address:
                continue
            targets.append(
                EndpointDispatchTarget(
                    container_id=state.container_id,
                    address=address,
                    route=next(
                        (item for item in address_map.routes if item.port == self.endpoint_port),
                        None,
                    ),
                )
            )
        return targets

    async def _is_ready(self, target: EndpointDispatchTarget, stub_id: str) -> bool:
        # `Running` means the container started, not that the runner bound its
        # port, and the load sort would otherwise favour the container nothing
        # has reached yet.
        route = target.route
        return await self.readiness_probe.is_ready(
            container_id=target.container_id,
            stub_id=stub_id,
            address=target.address,
            route_id=route.route_id if route is not None else "",
            port=self.endpoint_port,
            health_path=CONTAINER_HEALTH_PATH,
        )


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
