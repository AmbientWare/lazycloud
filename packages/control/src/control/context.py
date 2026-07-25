from __future__ import annotations

from pathlib import Path
from typing import Protocol

from database.types import DatabaseSession
from shared.identity import WorkspaceRecord

from database import DatabaseClient


class ControlPaths(Protocol):
    @property
    def root(self) -> Path: ...


class ControlContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    @property
    def paths(self) -> ControlPaths: ...

    def workspace(
        self, session: DatabaseSession, workspace: str = "default"
    ) -> WorkspaceRecord: ...
