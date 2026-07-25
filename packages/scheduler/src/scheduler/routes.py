from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shared.routing import AgentBackendRoute
from shared.scheduling import SchedulerBackendRoute

from scheduler.state import (
    RedisSchedulerContainerRepository,
)


class BackendRouteReader(Protocol):
    def get(self, route_id: str) -> AgentBackendRoute | None: ...


@dataclass(slots=True)
class SchedulerBackendRouteResolver:
    database_routes: BackendRouteReader
    containers: RedisSchedulerContainerRepository | None

    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None:
        route = self._scheduler_route(route_id)
        if route is not None:
            return _agent_route_from_scheduler(route)
        return self.database_routes.get(route_id)

    def _scheduler_route(self, route_id: str) -> SchedulerBackendRoute | None:
        if self.containers is None:
            return None
        container_id = _container_id_from_route_id(route_id)
        if not container_id:
            return None

        worker_address = self.containers.get_worker_address(container_id)
        if (
            worker_address is not None
            and worker_address.route is not None
            and worker_address.route.route_id == route_id
        ):
            return worker_address.route

        container_address = self.containers.get_container_address(container_id)
        if (
            container_address is not None
            and container_address.route is not None
            and container_address.route.route_id == route_id
        ):
            return container_address.route

        address_map = self.containers.get_container_address_map(container_id)
        return next((route for route in address_map.routes if route.route_id == route_id), None)


def _container_id_from_route_id(route_id: str) -> str:
    parts = route_id.split(":")
    return parts[2] if len(parts) == 5 else ""


def _agent_route_from_scheduler(route: SchedulerBackendRoute) -> AgentBackendRoute:
    return AgentBackendRoute(
        route_id=route.route_id,
        workspace_id=route.workspace_id,
        pool_name=route.pool_name,
        machine_id=route.machine_id,
        worker_id=route.worker_id,
        container_id=route.container_id,
        kind=route.kind,
        port=route.port,
        protocol=route.protocol,
        transport=route.transport,
        local_target=route.local_target,
        proxy_target=route.proxy_target,
        state=route.state,
        error=route.error,
        updated_at=route.updated_at,
    )


__all__ = ["SchedulerBackendRouteResolver"]
