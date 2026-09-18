from __future__ import annotations

from typing import Protocol

from database.repositories.identity import WorkspaceRecord
from database.types import DatabaseSession
from shared.placement import Placement


class PlacementResolver(Protocol):
    """Decides where a workload of a workspace runs, inside the caller's session."""

    def resolve_placement(
        self, session: DatabaseSession, workspace: WorkspaceRecord, machine: str
    ) -> Placement: ...


__all__ = ["PlacementResolver"]
