from __future__ import annotations

from pydantic import field_validator

from shared.capacity import MachinePool
from shared.contracts import ContractModel
from shared.enums import StringEnum

BACKEND_ROUTE_ADDRESS_SCHEME = "route"


class PrivateUnitFallback(StringEnum):
    """What a workload does when the unit it named has no capacity to give it."""

    Internal = "internal"
    Wait = "wait"
    Fail = "fail"


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


class AgentBackendRoute(ContractModel):
    route_id: str
    enrollment_id: str = ""
    workspace_id: str = ""
    pool: MachinePool = MachinePool("")
    capacity_owner_id: str = ""
    machine_id: str = ""
    worker_id: str = ""
    container_id: str = ""
    kind: BackendRouteKind = BackendRouteKind.Container
    port: int = 0
    protocol: BackendRouteProtocol = BackendRouteProtocol.Tcp
    local_target: str = ""
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
    "PrivateUnitFallback",
    "parse_backend_route_address",
]
