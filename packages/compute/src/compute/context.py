from __future__ import annotations

from typing import Protocol

from database.types import DatabaseSession
from shared.identity import WorkspaceRecord

from database import DatabaseClient


class ComputeContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    def default_workspace_id(self, session: DatabaseSession) -> str: ...

    def workspace(
        self, session: DatabaseSession, workspace: str = "default"
    ) -> WorkspaceRecord: ...
