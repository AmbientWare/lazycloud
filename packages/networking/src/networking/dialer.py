from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Protocol

import grpc
import grpc.aio
from pydantic import Field
from shared.contracts import ContractModel
from shared.http.agent_tunnel import TUNNEL_OPEN_TIMEOUT_SECONDS, TunnelRouteRequest
from shared.routing import AgentBackendRoute, BackendRouteState

from networking.tunnel_client import TunnelRouteClient

DEFAULT_BACKEND_ROUTE_DIAL_TIMEOUT_SECONDS = 30.0
DEFAULT_BACKEND_ROUTE_READY_POLL_SECONDS = 0.25


class BackendRouteUnavailable(ConnectionError):
    pass


class BackendRouteResolver(Protocol):
    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None: ...


class BackendRouteDialerConfig(ContractModel):
    timeout_seconds: float = Field(default=DEFAULT_BACKEND_ROUTE_DIAL_TIMEOUT_SECONDS, gt=0)
    ready_poll_seconds: float = Field(default=DEFAULT_BACKEND_ROUTE_READY_POLL_SECONDS, gt=0)


@dataclass(slots=True)
class BackendRouteDialer:
    tunnel: TunnelRouteClient
    resolver: BackendRouteResolver
    config: BackendRouteDialerConfig = field(default_factory=BackendRouteDialerConfig)

    def dial_backend_route(
        self, route_id: str, *, timeout_seconds: float | None = None
    ) -> socket.socket:
        timeout = (
            min(timeout_seconds, self.config.timeout_seconds)
            if timeout_seconds is not None
            else self.config.timeout_seconds
        )
        deadline = time.monotonic() + timeout
        while True:
            route = self.resolve_ready_route(route_id, deadline=deadline)
            try:
                return self.dial_route(route, deadline=deadline)
            except grpc.aio.AioRpcError as exc:
                if exc.code() not in (
                    grpc.StatusCode.UNAVAILABLE,
                    grpc.StatusCode.ABORTED,
                    grpc.StatusCode.NOT_FOUND,
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                ):
                    raise BackendRouteUnavailable(exc.details()) from exc
                failure = exc
            except (ConnectionError, TimeoutError) as exc:
                failure = exc
            if time.monotonic() + self.config.ready_poll_seconds >= deadline:
                raise BackendRouteUnavailable(
                    f"Backend route {route_id} could not connect before its deadline"
                ) from failure
            # No caller receives a socket until the agent acknowledges the destination.
            time.sleep(self.config.ready_poll_seconds)

    def dial_route(
        self, route: AgentBackendRoute, *, deadline: float | None = None
    ) -> socket.socket:
        if route.state is not BackendRouteState.Ready:
            raise BackendRouteUnavailable(f"Backend route {route.route_id} is {route.state.value}")
        if not route.enrollment_id:
            raise BackendRouteUnavailable("Backend route has no enrolled machine")
        timeout = (
            max(0, deadline - time.monotonic())
            if deadline is not None
            else self.config.timeout_seconds
        )
        return self.tunnel.connect(
            TunnelRouteRequest(
                workspace_id=route.workspace_id,
                enrollment_id=route.enrollment_id,
                route_id=route.route_id,
            ),
            min(timeout, TUNNEL_OPEN_TIMEOUT_SECONDS),
        )

    def resolve_ready_route(self, route_id: str, *, deadline: float) -> AgentBackendRoute:
        while True:
            route = self.resolver.get_backend_route(route_id)
            if route is None:
                raise BackendRouteUnavailable(f"Backend route {route_id} was not found")
            if route.state is BackendRouteState.Ready:
                return route
            if route.state is not BackendRouteState.Opening:
                raise BackendRouteUnavailable(f"Backend route {route_id} is {route.state.value}")
            if time.monotonic() + self.config.ready_poll_seconds >= deadline:
                raise TimeoutError(f"Backend route {route_id} is {route.state.value}")
            time.sleep(self.config.ready_poll_seconds)


def split_host_port(address: str) -> tuple[str, int]:
    host, separator, port_text = address.rpartition(":")
    if not separator:
        return "", 0
    try:
        port = int(port_text)
    except ValueError:
        return "", 0
    return host.strip("[]"), port
