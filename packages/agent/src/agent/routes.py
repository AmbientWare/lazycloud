from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from shared.http.agent_tunnel import AgentRouteUpdate
from shared.http.errors import HttpApiError, HttpTransportError
from shared.routing import BackendRouteState

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentTunnelRoute:
    route_id: str
    local_target: str
    state: BackendRouteState


@dataclass(frozen=True, slots=True)
class AgentRouteSnapshot:
    revision: int
    routes: Sequence[AgentTunnelRoute]


@dataclass(slots=True)
class AgentRoutes:
    load: Callable[[], AgentRouteSnapshot]
    report_ready: Callable[[AgentTunnelRoute], None]
    targets: dict[str, tuple[str, int]] = field(default_factory=dict, init=False)
    changed: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    revision: int = field(default=-1, init=False)
    _refresh_required: bool = field(default=True, init=False)
    _updates: dict[int, AgentRouteUpdate] = field(default_factory=dict, init=False)
    _known: dict[str, AgentTunnelRoute] = field(default_factory=dict, init=False)
    _probes: dict[str, tuple[AgentTunnelRoute, asyncio.Task[None]]] = field(
        default_factory=dict, init=False
    )
    _probe_slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(16), init=False
    )

    async def run(self) -> None:
        try:
            async with asyncio.TaskGroup() as probes:
                while True:
                    self.changed.clear()
                    try:
                        if self._refresh_required:
                            self._refresh_required = False
                            self._updates.clear()
                            snapshot = await asyncio.to_thread(self.load)
                            self._known = {route.route_id: route for route in snapshot.routes}
                            self.revision = snapshot.revision
                        for revision in sorted(self._updates):
                            update = self._updates.pop(revision)
                            if revision <= self.revision:
                                continue
                            if revision != self.revision + 1:
                                self._refresh_required = True
                                break
                            route = update.route
                            if route is None:
                                self._known.pop(update.route_id, None)
                            else:
                                self._known[update.route_id] = AgentTunnelRoute(
                                    route.route_id, route.local_target, route.state
                                )
                            self.revision = revision
                        if self._refresh_required:
                            continue
                        await self._refresh(tuple(self._known.values()), probes)
                    except (OSError, HttpApiError, HttpTransportError):
                        self._refresh_required = True
                        LOGGER.warning("Agent route refresh failed", exc_info=True)
                        await asyncio.sleep(1.0)
                        continue
                    await self.changed.wait()
        finally:
            tasks = [task for _, task in self._probes.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._probes.clear()
            self.targets.clear()
            self._known.clear()
            self._updates.clear()

    def accept_update(self, update: AgentRouteUpdate | None) -> None:
        if update is None or len(self._updates) >= 1024:
            self._refresh_required = True
        elif update.revision > self.revision:
            if update.route is not None and update.route.route_id != update.route_id:
                raise ValueError("Agent route notification identity mismatch")
            self._updates[update.revision] = update
        else:
            return
        self.changed.set()

    def observe_revision(self, revision: int) -> None:
        if revision != self.revision:
            self._refresh_required = True
            self.changed.set()

    async def _refresh(self, routes: Sequence[AgentTunnelRoute], probes: asyncio.TaskGroup) -> None:
        current = {
            route.route_id: route
            for route in routes
            if route.state is not BackendRouteState.Closing
        }
        cancelled: list[asyncio.Task[None]] = []
        for route_id, (previous, task) in list(self._probes.items()):
            if current.get(route_id) != previous:
                task.cancel()
                cancelled.append(task)
                del self._probes[route_id]
        await asyncio.gather(*cancelled, return_exceptions=True)
        targets: dict[str, tuple[str, int]] = {}
        for route in current.values():
            target = urlsplit(f"//{route.local_target}")
            if (
                target.username is not None
                or target.password is not None
                or target.path
                or target.query
                or target.fragment
                or not target.hostname
                or target.port is None
            ):
                raise ValueError("Agent route requires a registered host and port")
            targets[route.route_id] = (target.hostname, target.port)
            if route.state is not BackendRouteState.Ready and route.route_id not in self._probes:
                self._probes[route.route_id] = (
                    route,
                    probes.create_task(self._probe(route, targets[route.route_id])),
                )
        self.targets.clear()
        self.targets.update(targets)

    async def _probe(self, route: AgentTunnelRoute, target: tuple[str, int]) -> None:
        while True:
            try:
                async with self._probe_slots:
                    async with asyncio.timeout(0.25):
                        _, writer = await asyncio.open_connection(*target)
                        writer.close()
                        await writer.wait_closed()
                    await asyncio.to_thread(self.report_ready, route)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            except (HttpApiError, HttpTransportError):
                LOGGER.warning("Agent route readiness report failed route_id=%s", route.route_id)
                await asyncio.sleep(1.0)
                continue
            return
