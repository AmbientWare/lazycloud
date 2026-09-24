from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

import anyio
import pytest
from api.server.services import ApiServices
from execution.pods.proxy import PodProxySession, PodProxyTarget


@dataclass
class _Counters:
    total: int = 1
    container: int = 1

    async def container_connections(
        self, workspace_id: str, stub_id: str, container_id: str
    ) -> int:
        return self.container

    async def increment_container_connections(
        self, workspace_id: str, stub_id: str, container_id: str, *, keep_warm_seconds: int | None
    ) -> int:
        self.container += 1
        return self.container

    async def decrement_container_connections(
        self, workspace_id: str, stub_id: str, container_id: str, *, keep_warm_seconds: int | None
    ) -> int:
        await asyncio.sleep(0.01)  # a Redis round trip, where cancellation lands
        self.container -= 1
        return self.container

    async def increment_total_connections(self, workspace_id: str, stub_id: str) -> int:
        self.total += 1
        return self.total

    async def decrement_total_connections(self, workspace_id: str, stub_id: str) -> int:
        await asyncio.sleep(0.01)
        self.total -= 1
        return self.total


@pytest.mark.anyio
async def test_a_dropped_client_still_releases_its_pod_connection(
    async_services: ApiServices,
) -> None:
    counters = _Counters()
    service = replace(async_services.pod_service, pod_proxy_connections=counters)
    session = PodProxySession(
        workspace_id="workspace",
        stub_id="stub",
        target=PodProxyTarget(container_id="container", address=""),
        keep_warm_seconds=60,
    )

    # A client going away cancels the handler, and it finishes the session
    # inside that cancelled scope.
    with anyio.CancelScope() as scope:
        try:
            scope.cancel()
            await anyio.sleep(1)
        finally:
            await service.finish_pod_proxy(session)
    await asyncio.sleep(0.1)
    assert (counters.total, counters.container) == (0, 0)

    await service.finish_pod_proxy(session)
    assert (counters.total, counters.container) == (0, 0)
