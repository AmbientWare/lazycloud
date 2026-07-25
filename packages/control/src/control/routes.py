from __future__ import annotations

from dataclasses import dataclass

from database.repositories.orchestration import RouteRepository
from shared.routing import AgentBackendRoute

from control.context import ControlContext


@dataclass(slots=True)
class RouteService:
    context: ControlContext

    def get(self, route_id: str) -> AgentBackendRoute | None:
        """System routing lookup used by the scheduler and route proxy."""
        with self.context.database.session() as session:
            return RouteRepository(session).get_across_workspaces(route_id)

    def list(self, *, workspace_id: str) -> list[AgentBackendRoute]:
        with self.context.database.session() as session:
            return RouteRepository(session).list(workspace_id=workspace_id)

    def list_across_workspaces(self) -> list[AgentBackendRoute]:
        """System listing for route reconciliation."""
        with self.context.database.session() as session:
            return RouteRepository(session).list_across_workspaces()
