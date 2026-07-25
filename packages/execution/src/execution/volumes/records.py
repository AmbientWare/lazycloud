from __future__ import annotations

from dataclasses import dataclass

from database.repositories.storage import VolumeRepository
from observability.workspace_changes import WorkspaceChangePublisher
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.volumes import VolumeRecord

from execution.context import ExecutionContext


@dataclass(slots=True)
class VolumeService:
    context: ExecutionContext
    workspace_changes: WorkspaceChangePublisher | None = None

    def create(self, name: str, *, workspace: str = "default") -> VolumeRecord:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            existing = VolumeRepository(session).records.get(name, workspace_id=workspace_id)
            saved = VolumeRepository(session).create(name, workspace_id=workspace_id)
        self._publish_change(
            workspace_id,
            name,
            WorkspaceChangeType.Created if existing is None else WorkspaceChangeType.Updated,
        )
        return saved

    def list(self, *, workspace: str = "default") -> list[VolumeRecord]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = VolumeRepository(session).list(workspace_id=workspace_id)
        records.sort(key=lambda item: item.name)
        return records

    def get(self, name: str, *, workspace: str = "default") -> VolumeRecord:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            record = VolumeRepository(session).records.get(name, workspace_id=workspace_id)
        if record is None:
            return self.create(name, workspace=workspace)
        return record

    def delete(
        self,
        name: str,
        *,
        workspace: str = "default",
    ) -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            VolumeRepository(session).records.delete(name, workspace_id=workspace_id)
        self._publish_change(workspace_id, name, WorkspaceChangeType.Deleted)

    def delete_for_workspace_deletion(self, name: str, *, workspace_id: str) -> None:
        """Delete an existing volume record under workspace deletion authority."""
        with self.context.database.session() as session:
            VolumeRepository(session).delete_for_workspace_deletion(
                name,
                workspace_id=workspace_id,
            )

    def _publish_change(
        self,
        workspace_id: str,
        name: str,
        change: WorkspaceChangeType,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.StorageVolumes,
            change=change,
            resource_id=name,
        )
