from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import ProviderRepository
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.errors import ConflictError, InvalidInputError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import WorkspaceStatus
from shared.provider_config import ProviderConfig, ProviderKind
from shared.timestamps import utc_now

from compute.context import ComputeContext


@dataclass(slots=True)
class ProviderConfigService:
    context: ComputeContext
    workspace_changes: WorkspaceChangePublisher | None = None

    def set(
        self,
        name: str,
        *,
        kind: ProviderKind | str = ProviderKind.Aws,
        enabled: bool = True,
        priority: int = 100,
        config: Mapping[str, JsonValue] | None = None,
        labels: Mapping[str, str] | None = None,
        workspace: str = "default",
    ) -> ProviderConfig:
        try:
            provider_kind = ProviderKind(kind)
        except ValueError as exc:
            raise InvalidInputError(
                "production provider configuration currently supports only AWS; "
                "self-hosted machines use durable agent enrollment"
            ) from exc
        if config is not None and "provider_kind" in config:
            raise InvalidInputError(
                "provider kind must be selected by the provider record, not provider_kind config"
            )
        record = ProviderConfig(
            name=name,
            kind=provider_kind,
            enabled=enabled,
            priority=priority,
            config=dict(config or {}),
            labels=dict(labels or {}),
        )
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = ProviderRepository(session)
            existing = repository.records.get(name, workspace_id=workspace_id)
            if existing is not None:
                record.created_at = existing.created_at
            record.updated_at = utc_now()
            saved = repository.upsert(record, workspace_id=workspace_id)
        self._publish_change(
            workspace_id=workspace_id,
            change=(
                WorkspaceChangeType.Created if existing is None else WorkspaceChangeType.Updated
            ),
            resource_id=name,
        )
        return saved

    def get(self, name: str, *, workspace: str = "default") -> ProviderConfig:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            record = ProviderRepository(session).records.get(name, workspace_id=workspace_id)
        if record is None:
            msg = f"provider config not found: {name}"
            raise KeyError(msg)
        return record

    def list(
        self,
        *,
        enabled: bool | None = None,
        workspace: str = "default",
    ) -> list[ProviderConfig]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = ProviderRepository(session).list(workspace_id=workspace_id)
        if enabled is not None:
            records = [item for item in records if item.enabled is enabled]
        records.sort(key=lambda item: (item.priority, item.name))
        return records

    def list_for_workspace_deletion(
        self,
        workspace_id: str,
        *,
        enabled: bool | None = None,
    ) -> list[ProviderConfig]:
        with self.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(
                    f"workspace provider cleanup requires deleting state: {workspace_id}"
                )
            records = ProviderRepository(session).list(workspace_id=workspace_id)
        if enabled is not None:
            records = [item for item in records if item.enabled is enabled]
        records.sort(key=lambda item: (item.priority, item.name))
        return records

    def delete(self, name: str, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = ProviderRepository(session)
            existing = repository.records.get(name, workspace_id=workspace_id)
            repository.records.delete(name, workspace_id=workspace_id)
        if existing is not None:
            self._publish_change(
                workspace_id=workspace_id,
                change=WorkspaceChangeType.Deleted,
                resource_id=name,
            )

    def _publish_change(
        self,
        *,
        workspace_id: str,
        change: WorkspaceChangeType,
        resource_id: str,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeProviders,
            change=change,
            resource_id=resource_id,
        )
