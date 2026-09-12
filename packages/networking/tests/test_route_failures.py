from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from coordination.agent_connections import RedisAgentConnectionDirectory
from networking.dialer import BackendRouteDialer, BackendRouteUnavailable
from networking.tunnel_client import TunnelRouteClient
from networking.tunnel_tls import TunnelCredentials
from shared.routing import AgentBackendRoute, BackendRouteState
from tests.real_redis import RealRedisActors


@dataclass(slots=True)
class _Route:
    value: AgentBackendRoute | None

    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None:
        return self.value if self.value is None or self.value.route_id == route_id else None


def test_backend_route_rejects_missing_and_degraded_routes(
    real_redis_actors: RealRedisActors,
    tmp_path: Path,
) -> None:
    cases: list[tuple[AgentBackendRoute | None, str]] = [
        (None, "not found"),
        (AgentBackendRoute(route_id="unavailable", state=BackendRouteState.Degraded), "degraded"),
    ]
    with closing(
        TunnelRouteClient(
            RedisAgentConnectionDirectory(real_redis_actors.client()),
            TunnelCredentials(tmp_path / "control.key", tmp_path / "control.json"),
            "localhost",
        )
    ) as tunnel:
        for route, expected in cases:
            with pytest.raises(BackendRouteUnavailable, match=expected):
                BackendRouteDialer(tunnel=tunnel, resolver=_Route(route)).dial_backend_route(
                    "unavailable"
                )
