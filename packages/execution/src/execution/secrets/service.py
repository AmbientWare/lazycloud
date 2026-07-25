from __future__ import annotations

from dataclasses import dataclass

from database.records.identity import SecretStorageRecord
from database.repositories.identity import SecretRepository
from observability.events import EventService
from observability.workspace_changes import WorkspaceChangePublisher
from shared.errors import NotFoundError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import WorkspaceRecord
from shared.secrets import SecretRecord

from execution.context import ExecutionContext
from execution.secrets.crypto import WorkspaceSecretCipher


@dataclass(slots=True)
class SecretService:
    context: ExecutionContext
    events: EventService
    workspace_changes: WorkspaceChangePublisher | None = None

    def create(self, name: str, value: str, *, workspace: str = "default") -> SecretRecord:
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            workspace_id = workspace_record.id
            stored = SecretRepository(session).create(
                name,
                WorkspaceSecretCipher.from_workspace(workspace_record).encrypt(name, value),
                workspace_id=workspace_id,
            )
            plaintext = self._plaintext_record(stored, value=value)
        self._emit_mutation(name, workspace_id=workspace_id, change=WorkspaceChangeType.Created)
        return plaintext

    def set(self, name: str, value: str, *, workspace: str = "default") -> SecretRecord:
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            workspace_id = workspace_record.id
            cipher = WorkspaceSecretCipher.from_workspace(workspace_record)
            stored, created = SecretRepository(session).set(
                name,
                cipher.encrypt(name, value),
                workspace_id=workspace_id,
            )
            plaintext = self._plaintext_record(stored, value=value)
        self._emit_mutation(
            name,
            workspace_id=workspace_id,
            change=WorkspaceChangeType.Created if created else WorkspaceChangeType.Updated,
        )
        return plaintext

    def update(self, name: str, value: str, *, workspace: str = "default") -> SecretRecord:
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            workspace_id = workspace_record.id
            stored = SecretRepository(session).update(
                name,
                WorkspaceSecretCipher.from_workspace(workspace_record).encrypt(name, value),
                workspace_id=workspace_id,
            )
            plaintext = self._plaintext_record(stored, value=value)
        self._emit_mutation(name, workspace_id=workspace_id, change=WorkspaceChangeType.Updated)
        return plaintext

    def get(self, name: str, *, workspace: str = "default") -> SecretRecord:
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            stored = SecretRepository(session).get(name, workspace_id=workspace_record.id)
            if stored is not None:
                record = self._decrypt_record(stored, workspace=workspace_record)
            else:
                record = None
        if record is None:
            msg = f"secret not found: {name}"
            raise NotFoundError(msg)
        return record

    def list(self, *, workspace: str = "default") -> list[SecretRecord]:
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            records = [
                self._decrypt_record(record, workspace=workspace_record)
                for record in SecretRepository(session).list(workspace_id=workspace_record.id)
            ]
        return records

    def delete(self, name: str, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            SecretRepository(session).delete(name, workspace_id=workspace_id)
        self.events.emit(
            "secret.deleted",
            resource_type="secret",
            resource_id=name,
            message=f"deleted secret {name}",
            workspace_id=workspace_id,
        )
        self._publish_change(workspace_id, name, WorkspaceChangeType.Deleted)

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
            topic=WorkspaceChangeTopic.StorageSecrets,
            change=change,
            resource_id=name,
        )

    def _emit_mutation(
        self,
        name: str,
        *,
        workspace_id: str,
        change: WorkspaceChangeType,
    ) -> None:
        self.events.emit(
            "secret.set",
            resource_type="secret",
            resource_id=name,
            message=f"set secret {name}",
            workspace_id=workspace_id,
        )
        self._publish_change(workspace_id, name, change)

    @staticmethod
    def _plaintext_record(record: SecretStorageRecord, *, value: str) -> SecretRecord:
        return SecretRecord(
            name=record.name,
            value=value,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def _decrypt_record(
        self,
        record: SecretStorageRecord,
        *,
        workspace: WorkspaceRecord,
    ) -> SecretRecord:
        cipher = WorkspaceSecretCipher.from_workspace(workspace)
        return self._plaintext_record(
            record,
            value=cipher.decrypt(record.name, record.ciphertext),
        )
