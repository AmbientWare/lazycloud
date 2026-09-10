from __future__ import annotations

import socket
from contextlib import closing
from dataclasses import dataclass

import pytest
from networking.dialer import (
    BackendConnection,
    BackendRouteDialer,
    BackendRouteDialerConfig,
    BackendRouteUnavailable,
    SocketBackendConnector,
)
from networking.routing import BackendRouteAuthenticator, backend_route_preface
from pydantic import SecretStr
from shared.routing import AgentBackendRoute, BackendRouteState, BackendRouteTransport

_AUTH_KEY = SecretStr("0123456789abcdef0123456789abcdef")


@dataclass
class _Routes:
    routes: list[AgentBackendRoute | None]

    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None:
        route = self.routes.pop(0) if len(self.routes) > 1 else self.routes[0]
        return route if route is None or route.route_id == route_id else None


class _InterruptedConnector(SocketBackendConnector):
    interrupted = False

    def connect(self, address: str, timeout_seconds: float) -> BackendConnection:
        if not self.interrupted:
            self.interrupted = True
            raise ConnectionResetError("connection interrupted before completion")
        return super().connect(address, timeout_seconds)


@pytest.mark.parametrize(
    "transport", [BackendRouteTransport.Direct, BackendRouteTransport.PrivateNetwork]
)
def test_backend_route_recovers_from_opening_and_interrupted_connection(
    transport: BackendRouteTransport,
) -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(1)
        route = AgentBackendRoute(
            route_id="recovering-route",
            state=BackendRouteState.Ready,
            transport=transport,
            proxy_target=f"127.0.0.1:{listener.getsockname()[1]}",
        )
        dialer = BackendRouteDialer(
            _Routes([route.model_copy(update={"state": BackendRouteState.Opening}), route]),
            config=BackendRouteDialerConfig(
                timeout_seconds=1, ready_poll_seconds=0.001, auth_key=_AUTH_KEY
            ),
            connector=_InterruptedConnector(),
        )
        with closing(dialer.dial_backend_route(route.route_id)) as connection:
            connection.sendall(b"workload\n")
            with listener.accept()[0] as peer:
                peer.settimeout(1)
                with peer.makefile("rb") as wire:
                    if transport is BackendRouteTransport.PrivateNetwork:
                        assert wire.readline() == backend_route_preface(
                            route.route_id,
                            BackendRouteAuthenticator(_AUTH_KEY).credential(route.route_id),
                        )
                    assert wire.readline() == b"workload\n"


def test_private_route_requires_authentication_before_connecting() -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(0.01)
        route = AgentBackendRoute(
            route_id="private",
            state=BackendRouteState.Ready,
            transport=BackendRouteTransport.PrivateNetwork,
            proxy_target=f"127.0.0.1:{listener.getsockname()[1]}",
        )
        dialer = BackendRouteDialer(
            _Routes([route]), config=BackendRouteDialerConfig(timeout_seconds=1)
        )
        with pytest.raises(RuntimeError, match="authenticator is required"):
            dialer.dial_backend_route(route.route_id)
        with pytest.raises(TimeoutError):
            listener.accept()


def test_backend_route_rejects_missing_and_degraded_routes() -> None:
    cases: list[tuple[AgentBackendRoute | None, str]] = [
        (None, "not found"),
        (AgentBackendRoute(route_id="unavailable", state=BackendRouteState.Degraded), "degraded"),
    ]
    for route, expected in cases:
        with pytest.raises(BackendRouteUnavailable, match=expected):
            BackendRouteDialer(
                _Routes([route]), config=BackendRouteDialerConfig(timeout_seconds=1)
            ).dial_backend_route("unavailable")
