"""Whether this control plane is reachable at the address it hands out.

Serving HTTP on loopback proves the process is alive and nothing else. The
address workers and agents actually dial is the one it publishes, and a control
plane that publishes an address it does not answer on fails silently: nodes
enrol, report ready, and then reach nothing, while every container reports
healthy. Checking the published address is what makes that loud.

Run as `python -m api.health_probe`.
"""

from __future__ import annotations

import sys
from urllib.error import URLError
from urllib.request import urlopen

from coordination.redis_client import RedisClient
from networking.control_plane_origin import RedisControlPlaneOriginRepository

_LOOPBACK_HEALTH = "http://127.0.0.1:9000/health"
_TIMEOUT_SECONDS = 3.0


def _probe(url: str) -> None:
    with urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
        response.read()


def main() -> int:
    try:
        _probe(_LOOPBACK_HEALTH)
    except (URLError, OSError) as exc:
        print(f"control plane is not serving on loopback: {exc}", file=sys.stderr)
        return 1

    try:
        origin = RedisControlPlaneOriginRepository(RedisClient.from_settings()).resolve()
    except Exception as exc:
        print(f"control plane has published no runtime origin: {exc}", file=sys.stderr)
        return 1

    try:
        _probe(f"{origin.rstrip('/')}/health")
    except (URLError, OSError) as exc:
        print(
            f"control plane published {origin} but does not answer there: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
