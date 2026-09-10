from __future__ import annotations

import socket
from dataclasses import dataclass

import pytest
from execution.shells.proxy import ShellBackendTarget
from gateway.shell_proxy import connect_shell_backend
from networking.dialer import (
    BackendRouteDialerConfig,
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


@pytest.mark.parametrize(
    "transport", [BackendRouteTransport.Direct, BackendRouteTransport.PrivateNetwork]
)
def test_shell_uses_authoritative_route_and_authenticates_before_protocol(
    transport: BackendRouteTransport,
) -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(1)
        route = AgentBackendRoute(
            route_id="shell-route",
            state=BackendRouteState.Ready,
            transport=transport,
            proxy_target=f"127.0.0.1:{listener.getsockname()[1]}",
        )
        target = ShellBackendTarget(
            container_id="container",
            stub_id="stub",
            address="127.0.0.1:1",
            route=route,
            worker_port=2222,
            buffer_size_bytes=32768,
            dial_timeout_seconds=1,
        )
        with connect_shell_backend(
            target,
            route_resolver=_Routes([route]),
            route_dialer_config=BackendRouteDialerConfig(timeout_seconds=1, auth_key=_AUTH_KEY),
        ) as connection:
            connection.sendall(b"shell-protocol\n")
            with listener.accept()[0] as peer:
                peer.settimeout(1)
                with peer.makefile("rb") as wire:
                    if transport is BackendRouteTransport.PrivateNetwork:
                        assert wire.readline() == backend_route_preface(
                            route.route_id,
                            BackendRouteAuthenticator(_AUTH_KEY).credential(route.route_id),
                        )
                    assert wire.readline() == b"shell-protocol\n"
