"""Where the control plane is actually reachable, published by the process that knows.

The origin a worker or an agent dials used to be configuration: a hostname typed
into a deployment file that had to match, exactly, whatever name Tailscale
granted the control plane's device. It did not match twice — once naming the
Compose container rather than the tailnet device, once because a stale device
held the unsuffixed name and the live one registered as `-1`. Both produced
workers that sat pending forever while every log stayed quiet.

The control plane knows its own address and nothing else reliably does, so it
publishes it and everything else reads it back — the control plane included, so
that no process resolves this a second way.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from coordination.redis_client import RedisClient
from shared.errors import UpstreamUnavailableError

DEFAULT_CONTROL_PLANE_ORIGIN_TTL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ControlPlaneOriginKeys:
    redis: RedisClient
    namespace: str = "control-plane"

    def runtime_origin(self) -> str:
        return self.redis.key(self.namespace, "runtime-origin")


@dataclass(init=False, slots=True)
class RedisControlPlaneOriginRepository:
    redis: RedisClient
    keys: ControlPlaneOriginKeys

    def __init__(self, redis: RedisClient, keys: ControlPlaneOriginKeys | None = None) -> None:
        self.redis = redis
        self.keys = keys or ControlPlaneOriginKeys(redis)

    def publish(
        self,
        origin: str,
        *,
        ttl_seconds: int = DEFAULT_CONTROL_PLANE_ORIGIN_TTL_SECONDS,
    ) -> str:
        normalized = origin.strip()
        if not normalized:
            raise ValueError("control-plane runtime origin is required")
        self.redis.set(self.keys.runtime_origin(), normalized, ex=max(ttl_seconds, 1))
        return normalized

    def resolve(self) -> str:
        """The origin the control plane last published.

        Absence is an error rather than a reason to fall back on configuration:
        the value a fallback would supply is exactly the hand-maintained one this
        exists to stop trusting, and it fails silently — nodes enrol, report
        ready, and only then cannot reach anything.
        """
        stored = self.redis.get(self.keys.runtime_origin())
        origin = str(stored).strip() if stored is not None else ""
        if not origin:
            raise UpstreamUnavailableError(
                "the control plane has not published a runtime origin; it is starting, "
                "or its tailnet device is not up"
            )
        return origin


def runtime_origin_for_host(configured_origin: str, host: str) -> str:
    """Put the device's real host into the configured origin.

    Scheme and port stay as configured because they are genuine deployment
    choices; the hostname does not, because the control plane is told it by the
    tailnet and cannot choose it. Substituting only the part that is discovered
    keeps one source for each fact.
    """
    resolved_host = host.strip().rstrip(".")
    if not resolved_host:
        return configured_origin.strip()
    parsed = urlsplit(configured_origin.strip())
    netloc = f"[{resolved_host}]" if ":" in resolved_host else resolved_host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


__all__ = [
    "DEFAULT_CONTROL_PLANE_ORIGIN_TTL_SECONDS",
    "ControlPlaneOriginKeys",
    "RedisControlPlaneOriginRepository",
    "runtime_origin_for_host",
]
