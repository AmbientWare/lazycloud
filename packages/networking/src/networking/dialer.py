from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Protocol

from compute.projection import normalize_backend_route_transport
from pydantic import Field, SecretStr
from shared.contracts import ContractModel
from shared.routing import AgentBackendRoute, BackendRouteState, BackendRouteTransport

from networking.routing import (
    BACKEND_ROUTE_ID_METADATA_KEY,
    BackendDialPlan,
    BackendRouteAuthenticator,
    backend_route_preface,
)

DEFAULT_BACKEND_ROUTE_DIAL_TIMEOUT_SECONDS = 30.0
DEFAULT_BACKEND_ROUTE_READY_POLL_SECONDS = 0.25


class BackendRouteUnavailable(Exception):
    """A route id names nothing that can currently carry traffic.

    Separate from a misconfigured dialer, which raises on its own and which no
    retry fixes: this one says the route was looked up and found wanting, so a
    caller holding other targets is free to pick another.
    """


class BackendConnection(Protocol):
    def sendall(self, data: bytes, /) -> None: ...

    def close(self) -> None: ...


class BackendRouteResolver(Protocol):
    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None: ...


class BackendConnector(Protocol):
    def connect(self, address: str, timeout_seconds: float) -> BackendConnection: ...


class BackendRouteDialerConfig(ContractModel):
    timeout_seconds: float = Field(default=DEFAULT_BACKEND_ROUTE_DIAL_TIMEOUT_SECONDS, gt=0)
    ready_poll_seconds: float = Field(default=DEFAULT_BACKEND_ROUTE_READY_POLL_SECONDS, gt=0)
    auth_key: SecretStr = SecretStr("")


@dataclass(slots=True)
class SocketBackendConnector:
    def connect(self, address: str, timeout_seconds: float) -> BackendConnection:
        host, port = split_host_port(address)
        if not host or port <= 0:
            msg = f"invalid backend address: {address}"
            raise OSError(msg)
        return socket.create_connection((host, port), timeout=timeout_seconds)


@dataclass(slots=True)
class BackendRouteDialer:
    resolver: BackendRouteResolver | None = None
    config: BackendRouteDialerConfig = field(default_factory=BackendRouteDialerConfig)
    connector: BackendConnector = field(default_factory=SocketBackendConnector)

    def dial_plan(self, plan: BackendDialPlan) -> BackendConnection:
        route_id = _backend_route_id(plan)
        if route_id:
            return self.dial_backend_route(route_id)
        if plan.target.port is None:
            msg = f"backend dial target has no port: {plan.target.url}"
            raise ValueError(msg)
        return self.connector.connect(
            f"{plan.target.host}:{plan.target.port}",
            self.config.timeout_seconds,
        )

    def dial_backend_route(self, route_id: str) -> BackendConnection:
        if self.resolver is None:
            msg = f"backend route resolver is required for {route_id}"
            raise RuntimeError(msg)
        deadline = time.monotonic() + self.config.timeout_seconds
        route = self.resolve_ready_route(route_id, deadline=deadline)
        return self.dial_route(route, deadline=deadline)

    def dial_route(
        self,
        route: AgentBackendRoute,
        *,
        deadline: float | None = None,
    ) -> BackendConnection:
        route_id = route.route_id
        if deadline is None:
            deadline = time.monotonic() + self.config.timeout_seconds
        state = route.state
        if state is not BackendRouteState.Ready:
            msg = f"backend route {route_id} is {state.value}"
            raise BackendRouteUnavailable(msg)
        if not route.proxy_target:
            msg = f"backend route {route_id} has no proxy target"
            raise BackendRouteUnavailable(msg)
        transport = _route_transport(route.transport)
        authenticator = (
            self._require_route_authenticator()
            if transport is not BackendRouteTransport.Direct
            else None
        )
        connection = self._dial_route_target(
            route,
            route_id,
            deadline=deadline,
        )
        if authenticator is not None:
            _write_route_preface(connection, route_id, authenticator)
        return connection

    def _require_route_authenticator(self) -> BackendRouteAuthenticator:
        if not self.config.auth_key.get_secret_value():
            raise RuntimeError("backend route authenticator is required")
        return BackendRouteAuthenticator(self.config.auth_key)

    def resolve_ready_route(self, route_id: str, *, deadline: float) -> AgentBackendRoute:
        while True:
            route = self.resolver.get_backend_route(route_id) if self.resolver is not None else None
            if route is None:
                msg = f"backend route {route_id} not found"
                raise BackendRouteUnavailable(msg)
            state = route.state
            if state is BackendRouteState.Ready:
                if not route.proxy_target:
                    msg = f"backend route {route_id} has no proxy target"
                    raise BackendRouteUnavailable(msg)
                return route
            if state is not BackendRouteState.Opening:
                msg = f"backend route {route_id} is {state.value}"
                raise BackendRouteUnavailable(msg)
            if time.monotonic() + self.config.ready_poll_seconds >= deadline:
                msg = f"backend route {route_id} is {state.value}"
                raise TimeoutError(msg)
            time.sleep(self.config.ready_poll_seconds)

    def _dial_route_target(
        self,
        route: AgentBackendRoute,
        route_id: str,
        *,
        deadline: float,
    ) -> BackendConnection:
        last_error: OSError | None = None
        while _remaining_seconds(deadline) > 0:
            try:
                return self.connector.connect(
                    route.proxy_target,
                    _remaining_seconds(deadline),
                )
            except OSError as exc:
                last_error = exc
                if _remaining_seconds(deadline) <= self.config.ready_poll_seconds:
                    break
                time.sleep(self.config.ready_poll_seconds)
        if last_error is not None:
            msg = f"backend route {route_id} dial timed out: {last_error}"
            raise TimeoutError(msg) from last_error
        msg = f"backend route {route_id} dial timed out"
        raise TimeoutError(msg)


def split_host_port(address: str) -> tuple[str, int]:
    host, separator, port_text = address.rpartition(":")
    if not separator:
        return ("", 0)
    try:
        port = int(port_text)
    except ValueError:
        return ("", 0)
    return (host.strip("[]"), port)


def _remaining_seconds(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.001)


def _write_route_preface(
    connection: BackendConnection,
    route_id: str,
    authenticator: BackendRouteAuthenticator,
) -> None:
    try:
        connection.sendall(backend_route_preface(route_id, authenticator.credential(route_id)))
    except Exception:
        connection.close()
        raise


def _backend_route_id(plan: BackendDialPlan) -> str:
    value = plan.metadata.get(BACKEND_ROUTE_ID_METADATA_KEY, "")
    return value.strip() if isinstance(value, str) else ""


def _route_transport(value: BackendRouteTransport | str) -> BackendRouteTransport:
    if isinstance(value, BackendRouteTransport):
        return value
    return normalize_backend_route_transport(str(value))
