from __future__ import annotations

import socket

from execution.shells.proxy import ShellBackendTarget
from networking.dialer import BackendRouteDialer
from shared.routing import parse_backend_route_address


def connect_shell_backend(
    target: ShellBackendTarget, *, route_dialer: BackendRouteDialer
) -> socket.socket:
    route_id = (
        target.route.route_id
        if target.route is not None
        else parse_backend_route_address(target.address)[0]
    )
    if not route_id:
        raise ConnectionError("Shell requires an authorized backend route")
    return route_dialer.dial_backend_route(route_id, timeout_seconds=target.dial_timeout_seconds)


__all__ = ["connect_shell_backend"]
