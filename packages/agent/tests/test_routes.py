from __future__ import annotations

import asyncio
import socket

import pytest
from agent.routes import AgentRoutes, AgentRouteSnapshot, AgentTunnelRoute
from shared.http.agent_tunnel import AgentRouteUpdate
from shared.routing import AgentBackendRoute, BackendRouteState


@pytest.mark.anyio
async def test_opening_route_is_probed_until_listening_without_another_snapshot() -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    route = AgentTunnelRoute("late", f"127.0.0.1:{port}", BackendRouteState.Opening)
    snapshot = AgentRouteSnapshot(1, [route])
    reports: list[AgentTunnelRoute] = []
    routes = AgentRoutes(lambda: snapshot, reports.append)
    task = asyncio.create_task(routes.run())
    server: asyncio.Server | None = None
    try:
        for _ in range(20):
            print(f"targets={routes.targets} reports={len(reports)} owner_done={task.done()}")
            if routes.targets:
                break
            await asyncio.sleep(0.01)
        assert routes.targets and not reports
        server = await asyncio.start_server(
            lambda reader, writer: writer.close(), "127.0.0.1", port
        )
        for _ in range(50):
            print(f"listening=True reports={len(reports)} owner_done={task.done()}")
            if reports:
                break
            await asyncio.sleep(0.01)
        assert reports == [route]

        routes.accept_update(AgentRouteUpdate(revision=2, route_id=route.route_id, route=None))
        for _ in range(20):
            print(f"removed=True targets={routes.targets} revision={routes.revision}")
            if not routes.targets:
                break
            await asyncio.sleep(0.01)
        assert routes.targets == {}

        replacement = AgentBackendRoute(
            route_id="replacement", local_target=route.local_target, state=BackendRouteState.Ready
        )
        snapshot = AgentRouteSnapshot(
            4,
            [AgentTunnelRoute(replacement.route_id, replacement.local_target, replacement.state)],
        )
        routes.accept_update(
            AgentRouteUpdate(revision=4, route_id=replacement.route_id, route=replacement)
        )
        for _ in range(20):
            print(f"revision_gap=True targets={routes.targets} revision={routes.revision}")
            if routes.revision == 4:
                break
            await asyncio.sleep(0.01)
        assert routes.targets == {"replacement": ("127.0.0.1", port)}
        routes.accept_update(AgentRouteUpdate(revision=3, route_id="replacement", route=None))
        assert routes.targets == {"replacement": ("127.0.0.1", port)}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if server is not None:
            server.close()
            await server.wait_closed()
