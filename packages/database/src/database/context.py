from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from foundation.ids import try_uuid
from shared.errors import NotFoundError
from shared.identity import WorkspaceRecord, WorkspaceStatus
from shared.paths import state_home

from database import DatabaseClient
from database.repositories.identity import WorkspaceRepository
from database.types import DatabaseSession


@dataclass(slots=True)
class ServicePaths:
    root: Path

    @classmethod
    def from_root(cls, root: Path | None = None) -> ServicePaths:
        resolved = root or state_home()
        resolved.mkdir(parents=True, exist_ok=True)
        return cls(resolved)

    def volume_path(self, name: str) -> Path:
        path = self.root / "volumes" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def build_path(self, build_id: str) -> Path:
        path = self.root / "builds" / build_id
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass(slots=True)
class ServiceContext:
    database: DatabaseClient
    paths: ServicePaths

    @classmethod
    def create(
        cls,
        database: DatabaseClient,
        *,
        root: Path | None = None,
        create_schema: bool = True,
    ) -> ServiceContext:
        if create_schema:
            database.create_schema()
        return cls(database=database, paths=ServicePaths.from_root(root))

    def workspace(self, session: DatabaseSession, workspace: str = "default") -> WorkspaceRecord:
        repository = WorkspaceRepository(session)
        workspace_id = try_uuid(workspace)
        record = (
            repository.get(workspace_id)
            if workspace_id is not None
            else repository.by_name(workspace)
        )
        if record is None or record.status is not WorkspaceStatus.Active:
            msg = f"workspace not found: {workspace}"
            raise NotFoundError(msg)
        return record

    def default_workspace_id(self, session: DatabaseSession) -> str:
        return self.workspace(session).id
