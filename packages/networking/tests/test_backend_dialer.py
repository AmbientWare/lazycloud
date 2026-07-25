from __future__ import annotations

import socket
from dataclasses import dataclass

from networking.dialer import BackendRouteDialer, BackendRouteDialerConfig
from networking.routing import (
    BackendRouteAuthenticator,
    backend_route_preface,
    build_backend_route_dial_plan,
)
from pydantic import SecretStr
from shared.routing import AgentBackendRoute, BackendRouteState, BackendRouteTransport

_ROUTE_ID = "local-route"
_ROUTE_AUTH_KEY = SecretStr("0123456789abcdef0123456789abcdef")


@dataclass(frozen=True, slots=True)
class _RouteResolver:
    route: AgentBackendRoute

    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None:
        return self.route if route_id == self.route.route_id else None


def test_backend_route_dialer_connects_to_local_tcp_and_cleans_up(
    free_tcp_port: int,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.settimeout(1)
    listener.bind(("127.0.0.1", free_tcp_port))
    listener.listen(1)
    route = AgentBackendRoute(
        route_id=_ROUTE_ID,
        workspace_id="workspace-one",
        pool_name="pool-one",
        machine_id="machine-one",
        transport=BackendRouteTransport.LocalDirect,
        proxy_target=f"127.0.0.1:{free_tcp_port}",
        state=BackendRouteState.Ready,
    )
    dialer = BackendRouteDialer(
        resolver=_RouteResolver(route),
        config=BackendRouteDialerConfig(
            timeout_seconds=1,
            ready_poll_seconds=0.001,
            auth_key=_ROUTE_AUTH_KEY,
        ),
    )
    connection = None
    accepted = None
    stream = None
    try:
        connection = dialer.dial_plan(build_backend_route_dial_plan(_ROUTE_ID))
        accepted = listener.accept()[0]
        stream = accepted.makefile("rb")
        authenticator = BackendRouteAuthenticator(_ROUTE_AUTH_KEY)
        assert stream.readline() == backend_route_preface(
            _ROUTE_ID,
            authenticator.credential(_ROUTE_ID),
        )
    finally:
        if stream is not None:
            stream.close()
        if accepted is not None:
            accepted.close()
        if connection is not None:
            connection.close()
        listener.close()
