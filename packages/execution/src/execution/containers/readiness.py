"""Whether a container's workload is serving, as distinct from started.

A container reaching `Running` says the runtime created and started it; it says
nothing about whether the process inside has bound its port. Routing on the
former sends traffic to a backend that is not there yet, so the proxy paths ask
this instead.

The probe itself dials the backend, which belongs to the gateway. What lives
here is the protocol the routing services depend on. There is deliberately no
always-ready implementation to fall back on: answering the one question this
exists to ask with a silent yes disables the property, so a service that reaches
a routing path without a probe refuses instead.
"""

from __future__ import annotations

from typing import Protocol

DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS = 2.0
# One probe per container per window rather than one per request: the endpoint
# dispatcher re-selects every 50ms while it waits for capacity, and the pod proxy
# every 250ms, so an uncached verdict would dial the backend tens of times a
# second per waiting caller.
DEFAULT_READINESS_CACHE_TTL_MS = 500


class ContainerReadiness(Protocol):
    """Ask whether one backend is serving.

    An empty `health_path` means a TCP connect, which proves only that something
    accepted the connection. A path names an HTTP probe the workload answers
    itself, which is the only form that tells a bound socket from a working one.
    """

    def is_ready(
        self,
        *,
        container_id: str,
        stub_id: str,
        address: str,
        route_id: str,
        port: int,
        health_path: str = "",
    ) -> bool: ...


__all__ = [
    "DEFAULT_READINESS_CACHE_TTL_MS",
    "DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS",
    "ContainerReadiness",
]
