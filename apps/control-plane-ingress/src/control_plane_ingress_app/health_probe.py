"""Whether the deployment is reachable at the address it advertises.

Listening locally proves this process started and nothing else. What matters is
that the address handed to every agent, worker, and container resolves, arrives
here, and finds a control plane behind it — a chain that fails silently, because
each link reports itself healthy while the whole is broken.

Run as `python -m control_plane_ingress_app.health_probe`.
"""

from __future__ import annotations

import sys
from urllib.error import URLError
from urllib.request import urlopen

from coordination.redis_client import RedisClient
from networking.control_plane_origin import RedisControlPlaneOriginRepository

_TIMEOUT_SECONDS = 3.0


def _probe(url: str) -> None:
    with urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
        response.read()


def main() -> int:
    try:
        origin = RedisControlPlaneOriginRepository(RedisClient.from_settings()).resolve()
    except Exception as exc:
        print(f"no control-plane origin is published: {exc}", file=sys.stderr)
        return 1

    try:
        _probe(f"{origin.rstrip('/')}/health")
    except (URLError, OSError) as exc:
        print(
            f"{origin} is published but no control plane answers through it: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
