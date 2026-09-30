from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from billing.admission import DatabaseBillingAdmission
from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.identity import (
    UserRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
    new_signing_key,
)
from identity.auth import TokenIssuer
from pydantic import JsonValue
from shared.aws_connections import AwsAccountConnection, AwsAccountConnectionPhase
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.identity import TokenKind, WorkspaceRecord, WorkspaceStatus, WorkspaceStorageConfig
from shared.timestamps import utc_now
from sqlalchemy.orm import Session
from storage.workspace_provisioning import (
    WorkspaceBucketProvisioner,
    WorkspaceStorageAlreadyExistsError,
)

from control.context import ControlContext
from control.models import WorkspaceCreateResult


class WorkspaceCreationAdmission(Protocol):
    """Account admission before creating durable state or storage."""

    def assert_may_create_workspace(self, session: Session, *, owner_user_id: str) -> None: ...


def _upsert_workspace_row(
    session: Session,
    name: str,
    *,
    connection_id: str | None = None,
    signing_key_prefix: str | None = None,
    primary_token_id: str | None = None,
    labels: dict[str, str] | None = None,
    metadata: Mapping[str, JsonValue] | None = None,
) -> WorkspaceRecord:
    repository = WorkspaceRepository(session)
    repository.lock_name(name)
    existing = repository.by_name(name)
    record = existing or WorkspaceRecord(id=str(uuid4()), name=name, connection_id=connection_id)
    if record.status is not WorkspaceStatus.Active:
        raise ConflictError(f"workspace is not active: {name}")
    if existing is not None:
        record = repository.lock_active_owner(existing.id, exclusive=True)
    if connection_id is not None and record.connection_id != connection_id:
        raise ConflictError(f"workspace already lives in another location: {name}")
    record.signing_key_prefix = signing_key_prefix or record.signing_key_prefix
    record.signing_key = record.signing_key or new_signing_key(record.signing_key_prefix)
    record.primary_token_id = primary_token_id or record.primary_token_id
    record.labels.update(labels or {})
    record.metadata.update(metadata or {})
    record.updated_at = utc_now()
    return repository.upsert(record)


def _workspace_name_from(preferred: str) -> str:
    """Normalize provider logins, which may start with digits or contain capitals."""
    lowered = "".join(
        character if (character.isascii() and (character.isalnum() or character in "-_")) else "-"
        for character in preferred.strip().lower()
    ).strip("-")
    if not lowered:
        return ""
    if not lowered[0].isalpha():
        lowered = f"w-{lowered}"
    return lowered[:63].rstrip("-_")


def _workspace_storage_available(storage: WorkspaceStorageConfig) -> bool:
    return bool(storage.bucket and storage.backend != "local")


def _require_ready_connection(
    session: Session, connection_id: str, *, owner_user_id: str
) -> AwsAccountConnection:
    connection = AwsAccountConnectionRepository(session).get(connection_id)
    if connection is None or connection.user_id != owner_user_id:
        raise NotFoundError(f"connected cloud account not found: {connection_id}")
    if connection.phase is not AwsAccountConnectionPhase.Ready:
        raise ConflictError(
            f"connected AWS account {connection.account_id} is {connection.phase.value}; "
            "a workspace can only be created there once it is ready"
        )
    return connection


@dataclass(slots=True)
class WorkspaceService:
    context: ControlContext
    storage: WorkspaceBucketProvisioner
    workspace_admission: WorkspaceCreationAdmission = field(
        default_factory=DatabaseBillingAdmission
    )

    def set_workspace(
        self,
        name: str,
        *,
        owner_user_id: str,
        connection_id: str | None = None,
        signing_key_prefix: str | None = None,
        primary_token_id: str | None = None,
        labels: dict[str, str] | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> WorkspaceRecord:
        """Commit workspace settings and ownership together."""
        if not owner_user_id:
            raise InvalidInputError("a workspace is owned by the account that creates it")
        with self.context.database.session() as session:
            record = _upsert_workspace_row(
                session,
                name,
                connection_id=connection_id,
                signing_key_prefix=signing_key_prefix,
                primary_token_id=primary_token_id,
                labels=labels,
                metadata=metadata,
            )
            WorkspaceMemberRepository(session).ensure_owner(
                workspace_id=record.id, user_id=owner_user_id
            )
            return record

    def create_workspace(
        self, name: str | None = None, *, owner_user_id: str, connection_id: str | None = None
    ) -> WorkspaceCreateResult:
        with self.context.database.session() as session:
            workspace = self._prepare_workspace(
                session, name or f"workspace-{uuid4()}", owner_user_id, connection_id
            )
        return self._finish_workspace(workspace)

    def _prepare_workspace(
        self, session: Session, name: str, owner_user_id: str, connection_id: str | None
    ) -> WorkspaceRecord:
        UserRepository(session).lock_active(owner_user_id, exclusive=True)
        repository = WorkspaceRepository(session)
        repository.lock_name(name)
        existing = repository.by_name(name)
        members = WorkspaceMemberRepository(session)
        if existing is None:
            self.workspace_admission.assert_may_create_workspace(
                session, owner_user_id=owner_user_id
            )
        else:
            owner = members.owner(existing.id)
            if owner is not None and owner.user_id != owner_user_id:
                raise ConflictError(f"workspace name is already owned: {name}")
        if connection_id is not None:
            connection = _require_ready_connection(
                session, connection_id, owner_user_id=owner_user_id
            )
            self.storage.assert_provisionable(connection)
        record = _upsert_workspace_row(session, name, connection_id=connection_id)
        members.ensure_owner(workspace_id=record.id, user_id=owner_user_id)
        return record

    def _finish_workspace(self, workspace: WorkspaceRecord) -> WorkspaceCreateResult:
        workspace = self.ensure_workspace_storage(workspace.id)
        issuer = TokenIssuer(self.context)
        raw_token = ""
        with self.context.database.session() as session:
            repository = WorkspaceRepository(session)
            workspace = repository.lock_active_owner(workspace.id, exclusive=True)
            if not workspace.primary_token_id:
                raw_token, token = issuer.issue(
                    session,
                    f"{workspace.name}-primary",
                    kind=TokenKind.WorkspacePrimary,
                    workspace_id=workspace.id,
                )
                workspace.primary_token_id = token.id
                workspace.updated_at = utc_now()
                workspace = repository.upsert(workspace)
        issuer.committed()
        return WorkspaceCreateResult(
            workspace_id=workspace.id, token=raw_token, workspace=workspace
        )

    def ensure_default_workspace(
        self, owner_user_id: str, preferred_name: str = ""
    ) -> WorkspaceRecord:
        with self.context.database.session() as session:
            UserRepository(session).lock_active(owner_user_id, exclusive=True)
            workspace = WorkspaceMemberRepository(session).owned_workspace(owner_user_id)
            if workspace is None:
                workspace = self._prepare_workspace(
                    session,
                    self._available_workspace_name(session, preferred_name, owner_user_id),
                    owner_user_id,
                    None,
                )
        if workspace.primary_token_id and workspace.storage.bucket:
            return workspace
        return self._finish_workspace(workspace).workspace

    def _available_workspace_name(
        self, session: Session, preferred: str, owner_user_id: str
    ) -> str:
        candidate = _workspace_name_from(preferred)
        candidates = (
            [candidate, *(f"{candidate}-{suffix}" for suffix in range(2, 10))] if candidate else []
        )
        repository = WorkspaceRepository(session)
        taken = repository.existing_names(candidates)
        for name in candidates:
            if name not in taken:
                repository.lock_name(name)
                if repository.by_name(name) is None:
                    return name
        return f"workspace-{owner_user_id}"

    def set_workspace_storage(
        self, workspace: str, storage: WorkspaceStorageConfig
    ) -> WorkspaceRecord:
        with self.context.database.session() as session:
            repository = WorkspaceRepository(session)
            workspace_id = self.context.workspace(session, workspace).id
            record = repository.lock_active_owner(workspace_id, exclusive=True)
            record.storage = storage
            record.updated_at = utc_now()
            return repository.upsert(record)

    def ensure_workspace_storage(self, workspace: str) -> WorkspaceRecord:
        """Provision the workspace's platform bucket idempotently."""
        record = self.get_workspace(workspace)
        if record.storage.bucket:
            return record
        return self._provision_storage(record)

    def create_workspace_storage(self, workspace: str) -> WorkspaceRecord:
        record = self.get_workspace(workspace)
        if _workspace_storage_available(record.storage):
            raise WorkspaceStorageAlreadyExistsError("workspace storage already exists")
        return self._provision_storage(record)

    def _provision_storage(self, record: WorkspaceRecord) -> WorkspaceRecord:
        storage = self.storage.provision(record)
        with self.context.database.session() as session:
            repository = WorkspaceRepository(session)
            current = repository.lock_active_owner(record.id, exclusive=True)
            if not _workspace_storage_available(current.storage):
                current.storage = storage
                current.updated_at = utc_now()
                current = repository.upsert(current)
            return current

    def list_workspaces(
        self, *, include_deleted: bool = False, include_deleting: bool = False
    ) -> list[WorkspaceRecord]:
        statuses = {WorkspaceStatus.Active}
        if include_deleting:
            statuses.add(WorkspaceStatus.Deleting)
        with self.context.database.session() as session:
            return sorted(
                WorkspaceRepository(session).list(statuses=None if include_deleted else statuses),
                key=lambda item: item.name,
            )

    def get_workspace(self, workspace_id_or_name: str = "default") -> WorkspaceRecord:
        with self.context.database.session() as session:
            return self.context.workspace(session, workspace_id_or_name)
