from __future__ import annotations

import socket
from contextlib import ExitStack, closing
from dataclasses import dataclass

import pytest
from networking.dialer import BackendRouteDialer, BackendRouteDialerConfig
from networking.routing import (
    BackendRouteAuthenticator,
    backend_route_preface,
)
from pydantic import SecretStr
from shared.compute_policy import MachinePool
from shared.routing import AgentBackendRoute, BackendRouteState, BackendRouteTransport

_ROUTE_ID = "local-route"
_ROUTE_AUTH_KEY = SecretStr("0123456789abcdef0123456789abcdef")


@dataclass(frozen=True, slots=True)
class _RouteResolver:
    route: AgentBackendRoute

    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None:
        return self.route if route_id == self.route.route_id else None


@pytest.mark.parametrize(
    "transport", [BackendRouteTransport.LocalDirect, BackendRouteTransport.PrivateNetwork]
)
def test_backend_route_dialer_authenticates_its_connection(
    transport: BackendRouteTransport,
) -> None:
    with ExitStack() as stack:
        listener = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        listener.settimeout(1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        route = AgentBackendRoute(
            route_id=_ROUTE_ID,
            workspace_id="workspace-one",
            pool=MachinePool("pool-one"),
            machine_id="machine-one",
            transport=transport,
            proxy_target=f"127.0.0.1:{listener.getsockname()[1]}",
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
        stack.enter_context(closing(dialer.dial_backend_route(_ROUTE_ID)))
        accepted = stack.enter_context(listener.accept()[0])
        accepted.settimeout(1)
        stream = stack.enter_context(accepted.makefile("rb"))
        authenticator = BackendRouteAuthenticator(_ROUTE_AUTH_KEY)
        assert stream.readline() == backend_route_preface(
            _ROUTE_ID,
            authenticator.credential(_ROUTE_ID),
        )
