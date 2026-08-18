"""Whether a container's workload is serving, as distinct from started.

A container reaching `Running` says the runtime created and started it; it says
nothing about whether the process inside has bound its port. Routing on the
former sends traffic to a backend that is not there yet, so the proxy paths ask
this instead.

The probe itself dials the backend, which belongs to the gateway. What lives
here is the protocol the routing services depend on, and the always-ready
implementation they fall back to when nothing has been wired.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import Field
from shared.contracts import ContractModel

DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS = 2.0
# One probe per container per window rather than one per request: the endpoint
# dispatcher re-selects every 50ms while it waits for capacity, and the pod proxy
# every 250ms, so an uncached verdict would dial the backend tens of times a
# second per waiting caller.
DEFAULT_READINESS_CACHE_TTL_MS = 500


class ContainerHealthCheck(ContractModel):
    """How to ask one container whether it is serving.

    An empty path means a TCP connect, which proves only that something accepted
    the connection. A path names an HTTP probe the workload answers itself, which
    is the only form that can distinguish a bound socket from a working one.
    """

    path: str = ""
    port: int = Field(default=0, ge=0, le=65535)

    @property
    def is_http(self) -> bool:
        return bool(self.path)


class ContainerReadiness(Protocol):
    def is_ready(
        self,
        *,
        container_id: str,
        stub_id: str,
        address: str,
        route_id: str,
        port: int,
        health_check: ContainerHealthCheck | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AlwaysReadyContainers:
    """The behavior of every caller before a probe existed.

    Kept as the unconfigured default so a service constructed without a probe
    routes exactly as it used to rather than refusing every container.
    """

    def is_ready(
        self,
        *,
        container_id: str,
        stub_id: str,
        address: str,
        route_id: str,
        port: int,
        health_check: ContainerHealthCheck | None = None,
    ) -> bool:
        _ = container_id, stub_id, address, route_id, port, health_check
        return True


__all__ = [
    "DEFAULT_READINESS_CACHE_TTL_MS",
    "DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS",
    "AlwaysReadyContainers",
    "ContainerHealthCheck",
    "ContainerReadiness",
]
