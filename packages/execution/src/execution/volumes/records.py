from __future__ import annotations

from dataclasses import dataclass

from database.repositories.storage import VolumeRepository
from observability.workspace_changes import WorkspaceChangePublisher
from shared.errors import ConflictError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.volumes import VolumeRecord

from execution.admission import PaymentAdmission
from execution.context import ExecutionContext


@dataclass(slots=True)
class VolumeService:
    context: ExecutionContext
    workspace_changes: WorkspaceChangePublisher | None = None

    def get_or_create(
        self,
        name: str,
        *,
        workspace: str = "default",
        admit: PaymentAdmission | None,
    ) -> VolumeRecord:
        """Resolve a volume, creating it if this is the first anybody has asked.

        One session across the lookup and the insert, so `admit` is answered in
        the transaction that would write the row. Split across two sessions a
        card removal or a failed payment landing between them would be admitted
        anyway, and the refusal would be the only kind that leaves something
        behind.

        The lookup is not a lock, so two containers mounting the same new name
        can both reach the insert; the repository settles that on the unique
        constraint and reports which call actually created the row.

        `admit` has no default, so every caller states its answer rather than
        inheriting one. `None` is a decision — the caller has already been
        judged, as a container building its mounts was moments earlier — and a
        new creation path has to say which it is instead of being unguarded by
        omission.
        """

        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            volumes = VolumeRepository(session)
            existing = volumes.get(name, workspace_id=workspace_id)
            if existing is not None:
                if existing.deletion_requested_at is not None:
                    raise ConflictError(f"volume {name} is deleting")
                return existing
            if admit is not None:
                admit.assert_may_take_on_billed_work(session, workspace_id=workspace_id)
            saved, created = volumes.create(name, workspace_id=workspace_id)
        # Only what this call brought into existence. A concurrent start that
        # lost the insert announces nothing, because the winner already did.
        if created:
            self._publish_change(workspace_id, name, WorkspaceChangeType.Created)
        return saved

    def list(self, *, workspace: str = "default") -> list[VolumeRecord]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = VolumeRepository(session).list(workspace_id=workspace_id)
        records.sort(key=lambda item: item.name)
        return records

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
