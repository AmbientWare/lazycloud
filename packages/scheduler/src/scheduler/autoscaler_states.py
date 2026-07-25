from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from database.repositories.orchestration import AutoscalerStateRepository
from shared.autoscaler_state import AutoscalerStateRecord, AutoscalerTargetKind

from database import DatabaseClient


class AutoscalerStateContextPaths(Protocol):
    @property
    def root(self) -> Path: ...


class AutoscalerStateContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    @property
    def paths(self) -> AutoscalerStateContextPaths: ...


@dataclass(slots=True)
class AutoscalerStateService:
    context: AutoscalerStateContext

    def upsert(self, state: AutoscalerStateRecord) -> AutoscalerStateRecord:
        with self.context.database.session() as session:
            return AutoscalerStateRepository(session).upsert(state)

    def get(
        self,
        *,
        workspace_id: str,
        target_kind: AutoscalerTargetKind,
        target_id: str,
    ) -> AutoscalerStateRecord | None:
        with self.context.database.session() as session:
            return AutoscalerStateRepository(session).get(
                workspace_id=workspace_id,
                target_kind=target_kind,
                target_id=target_id,
            )

    def list(
        self,
        *,
        workspace_id: str | None = None,
        source: str | None = None,
    ) -> list[AutoscalerStateRecord]:
        with self.context.database.session() as session:
            repository = AutoscalerStateRepository(session)
            if workspace_id is None:
                return repository.list_across_workspaces(source=source)
            return repository.list(workspace_id=workspace_id, source=source)
