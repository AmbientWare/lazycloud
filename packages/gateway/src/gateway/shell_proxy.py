from __future__ import annotations

import socket
from dataclasses import dataclass

from execution.shells.proxy import ShellBackendTarget
from networking.dialer import (
    BackendRouteDialer,
    BackendRouteDialerConfig,
    BackendRouteResolver,
    TailnetPeerResolver,
    TailnetPeerWaiter,
)
from networking.routing import build_backend_route_dial_plan
from shared.routing import AgentBackendRoute, parse_backend_route_address


def connect_shell_backend(
    target: ShellBackendTarget,
    *,
    route_resolver: BackendRouteResolver | None = None,
    route_dialer_config: BackendRouteDialerConfig | None = None,
    tailnet_peer_waiter: TailnetPeerWaiter | None = None,
    tailnet_peer_resolver: TailnetPeerResolver | None = None,
) -> socket.socket:
    parsed_route_id, address_is_route = parse_backend_route_address(target.address)
    route_id = target.route.route_id if target.route is not None else parsed_route_id
    uses_route = bool(route_id) and (target.route is not None or address_is_route)
    resolver: BackendRouteResolver | None = None
    if uses_route:
        resolver = route_resolver or _SingleBackendRouteResolver(target.route)
    dialer = BackendRouteDialer(
        resolver=resolver,
        config=(route_dialer_config or BackendRouteDialerConfig()).model_copy(
            update={"timeout_seconds": target.dial_timeout_seconds}
        ),
        tailnet_peer_waiter=tailnet_peer_waiter,
        tailnet_peer_resolver=tailnet_peer_resolver,
    )
    connection = (
        dialer.dial_plan(build_backend_route_dial_plan(route_id))
        if uses_route
        else dialer.connector.connect(target.address, target.dial_timeout_seconds)
    )
    if not isinstance(connection, socket.socket):
        connection.close()
        msg = "shell backend dialer returned a non-socket connection"
        raise TypeError(msg)
    return connection


@dataclass(slots=True)
class _SingleBackendRouteResolver:
    route: AgentBackendRoute | None

    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None:
        if self.route is None or self.route.route_id != route_id:
            return None
        return AgentBackendRoute.model_validate(self.route.model_dump(mode="json"))


__all__ = ["connect_shell_backend"]
