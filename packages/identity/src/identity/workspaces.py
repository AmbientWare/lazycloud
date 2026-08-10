from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from database.repositories.identity import (
    TokenRepository,
    WorkspaceAuditCursor,
    WorkspaceAuditPage,
    WorkspaceAuditRepository,
    WorkspaceRepository,
)
from database.types import DatabaseSession
from pydantic import ValidationError
from shared.contracts import ContractModel
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import AuthTokenRecord, WorkspaceRecord, WorkspaceStatus
from shared.timestamps import utc_now
from sqlalchemy.exc import IntegrityError

from identity.auth import IdentityContext


@dataclass(frozen=True, slots=True)
class WorkspaceAuditResult:
    page: WorkspaceAuditPage
    next: str = ""


class _WorkspaceAuditCursorPayload(ContractModel):
    created_at: datetime
    id: UUID


class WorkspaceSettingsService:
    def __init__(self, context: IdentityContext) -> None:
        self.context = context

    def rename(
        self,
        workspace_id_or_name: str,
        *,
        name: str,
        actor: AuthTokenRecord,
    ) -> WorkspaceRecord:
        with self.context.database.session() as session:
            repository = WorkspaceRepository(session)
            workspace = _lock_active_workspace(repository, workspace_id_or_name)
            if workspace.name == name:
                return workspace
            existing = repository.by_name(name)
            if existing is not None:
                raise ConflictError(f"workspace name is already in use: {name}")
            previous_name = workspace.name
            workspace.name = name
            workspace.updated_at = utc_now()
            try:
                updated = repository.upsert(workspace)
                WorkspaceAuditRepository(session).append(
                    workspace_id=workspace.id,
                    action=WorkspaceAuditAction.WorkspaceRenamed,
                    actor=actor,
                    target_type=WorkspaceAuditTarget.Workspace,
                    target_id=workspace.id,
                    target_name=name,
                    summary=f"Renamed workspace from {previous_name} to {name}",
                    previous_value=previous_name,
                    new_value=name,
                )
                return updated
            except IntegrityError as exc:
                raise ConflictError(f"workspace name is already in use: {name}") from exc

    def audit_history(
        self,
        workspace_id_or_name: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> WorkspaceAuditResult:
        if limit < 1 or limit > 100:
            raise InvalidInputError("audit history limit must be between 1 and 100")
        decoded = _decode_cursor(cursor)
        with self.context.database.session() as session:
            workspace = self.context.workspace(session, workspace_id_or_name)
            page = WorkspaceAuditRepository(session).page(
                workspace_id=workspace.id,
                limit=limit,
                cursor=decoded,
            )
        return WorkspaceAuditResult(
            page=page,
            next=_encode_cursor(page.next) if page.next is not None else "",
        )


class WorkspaceDeletionIdentityService:
    """Own identity lifecycle transitions inside coordinator-owned transactions."""

    def __init__(self, context: IdentityContext) -> None:
        self.context = context

    def resolve(self, workspace_id_or_name: str) -> WorkspaceRecord:
        with self.context.database.session() as session:
            workspace = WorkspaceRepository(session).resolve_for_deletion(workspace_id_or_name)
        if workspace is None:
            raise NotFoundError(f"workspace not found: {workspace_id_or_name}")
        return workspace

    def lock_and_validate_begin(
        self,
        session: DatabaseSession,
        workspace_id: str,
        *,
        actor_workspace_id: str,
    ) -> WorkspaceRecord:
        repository = WorkspaceRepository(session)
        workspace = repository.lock_for_deletion(workspace_id)
        if workspace.status is WorkspaceStatus.Deleted:
            return workspace
        _validate_deletion_protections(
            repository,
            workspace,
            actor_workspace_id=actor_workspace_id,
        )
        if workspace.status not in {WorkspaceStatus.Active, WorkspaceStatus.Deleting}:
            raise ConflictError(f"workspace is not active: {workspace.id}")
        return workspace

    def mark_deleting(
        self,
        session: DatabaseSession,
        workspace: WorkspaceRecord,
    ) -> WorkspaceRecord:
        now = utc_now()
        TokenRepository(session).revoke_workspace_for_deletion(workspace.id, now=now)
        return WorkspaceRepository(session).mark_deleting(workspace)

    def finalize(
        self,
        session: DatabaseSession,
        workspace_id: str,
        *,
        actor: AuthTokenRecord,
    ) -> WorkspaceRecord:
        repository = WorkspaceRepository(session)
        workspace = repository.lock_for_deletion(workspace_id)
        if workspace.status is WorkspaceStatus.Deleted:
            return workspace
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace finalization requires deleting state: {workspace.id}")
        repository.purge_owned_records(workspace.id)
        deleted = repository.tombstone(workspace)
        repository.delete_identity_records(workspace.id)
        WorkspaceAuditRepository(session).append_workspace_deleted(
            workspace_id=deleted.id,
            actor=actor,
            target_name=deleted.name,
        )
        return deleted


def _lock_active_workspace(
    repository: WorkspaceRepository,
    workspace_id_or_name: str,
) -> WorkspaceRecord:
    workspace = repository.resolve_for_deletion(workspace_id_or_name)
    if workspace is None:
        raise NotFoundError(f"workspace not found: {workspace_id_or_name}")
    return repository.lock_active_owner(workspace.id)


def _validate_deletion_protections(
    repository: WorkspaceRepository,
    workspace: WorkspaceRecord,
    *,
    actor_workspace_id: str,
) -> None:
    if workspace.name == "default":
        raise ConflictError("the default workspace cannot be deleted")
    if workspace.id == actor_workspace_id:
        raise ConflictError("the workspace that owns the current admin token cannot be deleted")
    active_workspaces = [
        item for item in repository.list() if item.status is WorkspaceStatus.Active
    ]
    if workspace.status is WorkspaceStatus.Active and len(active_workspaces) <= 1:
        raise ConflictError("the last workspace cannot be deleted")


def _encode_cursor(cursor: WorkspaceAuditCursor) -> str:
    payload = {
        "created_at": cursor.created_at.isoformat(),
        "id": cursor.id,
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()


def _decode_cursor(value: str | None) -> WorkspaceAuditCursor | None:
    if not value:
        return None
    try:
        payload = _WorkspaceAuditCursorPayload.model_validate_json(
            base64.urlsafe_b64decode(value.encode()),
            strict=True,
        )
        created_at = payload.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return WorkspaceAuditCursor(
            created_at=created_at,
            id=str(payload.id),
        )
    except (binascii.Error, ValidationError) as exc:
        raise InvalidInputError("invalid workspace audit cursor") from exc


__all__ = [
    "WorkspaceAuditResult",
    "WorkspaceDeletionIdentityService",
    "WorkspaceSettingsService",
]
