from __future__ import annotations

from pydantic import field_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum

BACKEND_ROUTE_ADDRESS_SCHEME = "route"


class BackendRouteTransport(StringEnum):
    Direct = "direct"
    TsnetRestricted = "tsnet_restricted"
    LocalDirect = "local_direct"


class BackendRouteState(StringEnum):
    Opening = "opening"
    Ready = "ready"
    Degraded = "degraded"
    Closing = "closing"


class BackendRouteKind(StringEnum):
    Worker = "worker"
    Container = "container"


class BackendRouteProtocol(StringEnum):
    Tcp = "tcp"
    Http = "http"


class RoutePrewarmDecision(StringEnum):
    Attempt = "attempt"
    EmptyTarget = "empty-target"
    Throttled = "throttled"
    NotReady = "not-ready"
    UnsupportedTransport = "unsupported-transport"


class AgentBackendRoute(ContractModel):
    route_id: str
    workspace_id: str = ""
    pool_name: str = ""
    machine_id: str = ""
    worker_id: str = ""
    container_id: str = ""
    kind: BackendRouteKind = BackendRouteKind.Container
    port: int = 0
    protocol: BackendRouteProtocol = BackendRouteProtocol.Tcp
    transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted
    local_target: str = ""
    proxy_target: str = ""
    state: BackendRouteState = BackendRouteState.Opening
    error: str = ""
    updated_at: int = 0

    @field_validator("port")
    @classmethod
    def route_port_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "backend route port cannot be negative"
            raise ValueError(msg)
        return value


def parse_backend_route_address(address: str) -> tuple[str, bool]:
    prefix = f"{BACKEND_ROUTE_ADDRESS_SCHEME}://"
    if not address.startswith(prefix):
        return ("", False)
    route_id = address.removeprefix(prefix)
    return (route_id, bool(route_id))


__all__ = [
    "BACKEND_ROUTE_ADDRESS_SCHEME",
    "AgentBackendRoute",
    "BackendRouteKind",
    "BackendRouteProtocol",
    "BackendRouteState",
    "BackendRouteTransport",
    "RoutePrewarmDecision",
    "parse_backend_route_address",
]
