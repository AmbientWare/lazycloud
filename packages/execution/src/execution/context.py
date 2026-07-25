from __future__ import annotations

from pathlib import Path
from typing import Protocol

from database.types import DatabaseSession
from shared.identity import WorkspaceRecord

from database import DatabaseClient


class ExecutionPaths(Protocol):
    @property
    def root(self) -> Path: ...


class ExecutionContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    @property
    def paths(self) -> ExecutionPaths: ...

    def workspace(
        self,
        session: DatabaseSession,
        workspace: str = "default",
    ) -> WorkspaceRecord: ...

    def default_workspace_id(self, session: DatabaseSession) -> str: ...
