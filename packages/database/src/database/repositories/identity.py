from __future__ import annotations

import secrets
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.mappers.identity import (
    auth_token_record_from_table,
    device_authorization_record_from_table,
    secret_storage_record_from_table,
    user_record_from_table,
    workspace_member_record_from_table,
)
from database.records.identity import (
    DeviceAuthorizationRecord as _DeviceAuthorizationRecord,
)
from database.records.identity import (
    SecretStorageRecord,
)
from database.repositories.common import (
    GlobalTableRepository,
    TableRepositoryConfig,
    WorkspaceTableRepository,
)
from database.tables.base import DatabaseBase
from database.tables.execution import EventTable
from database.tables.identity import (
    ConcurrencyLimitTable,
    CredentialTable,
    DeviceAuthorizationTable,
    IdentityAdminRecoveryRequestTable,
    IdentityBootstrapClaimTable,
    SecretTable,
    TokenTable,
    UserTable,
    WorkspaceAuditEventTable,
    WorkspaceMemberTable,
    WorkspaceStorageTable,
    WorkspaceTable,
)
from database.tables.observability import (
    UsageBillingContributionTable,
    UsageBillingWindowTable,
    UsageRecordTable,
)
from database.tables.source_cache import (
    SourceCacheCleanupTargetTable,
    WorkerCacheGenerationTable,
)
from pydantic import Field, JsonValue, TypeAdapter
from shared.contracts import ContractModel
from shared.errors import ConflictError, NotFoundError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import (
    AuthTokenRecord,
    ConcurrencyLimitRecord,
    DeviceAuthorizationStatus,
    PlatformRole,
    TokenKind,
    TokenStatus,
    UserRecord,
    UserStatus,
    WorkspaceMemberRecord,
    WorkspaceRecord,
    WorkspaceRole,
    WorkspaceStatus,
    WorkspaceStorageConfig,
)
from shared.timestamps import utc_now
from sqlalchemy import and_, case, delete, exists, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, class_mapper

_STRINGS_ADAPTER = TypeAdapter(list[str])


def new_signing_key(prefix: str | None = None) -> str:
    return f"{prefix or 'sign_'}{secrets.token_urlsafe(32)}"


class WorkspaceStorageRecord(ContractModel):
    id: str
    workspace_id: str
    bucket_name: str = ""
    endpoint_url: str = ""
    region: str = ""
    config: dict[str, JsonValue] = Field(default_factory=dict)


_INITIAL_ADMIN_CLAIM_KEY = "initial-admin"


@dataclass(frozen=True, slots=True)
class IdentityBootstrapClaim:
    request_id: str
    admin_token_id: str | None
    published_at: datetime | None


@dataclass(slots=True)
class IdentityBootstrapClaimRepository:
    session: Session

    def required(self) -> bool:
        return self.get() is None

    def get(self, *, lock: bool = False) -> IdentityBootstrapClaim | None:
        statement = select(IdentityBootstrapClaimTable).where(
            IdentityBootstrapClaimTable.claim_key == _INITIAL_ADMIN_CLAIM_KEY
        )
        if lock:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        if row is None:
            return None
        return IdentityBootstrapClaim(
            request_id=row.request_id,
            admin_token_id=row.admin_token_id,
            published_at=row.published_at,
        )

    def create(self, *, request_id: str) -> bool:
        """Atomically acquire the permanent singleton administrator claim.

        The unique primary key is the cross-process concurrency authority. A
        savepoint contains the losing insert so the caller's transaction stays
        usable and can return the existing conflict outcome.
        """
        if self.get() is not None:
            return False
        try:
            with self.session.begin_nested():
                self.session.add(
                    IdentityBootstrapClaimTable(
                        claim_key=_INITIAL_ADMIN_CLAIM_KEY,
                        request_id=request_id,
                    )
                )
                self.session.flush()
        except IntegrityError:
            if self.get() is None:
                raise
            return False
        return True

    def attach_admin_token(self, token_id: str) -> None:
        row = self.session.get(IdentityBootstrapClaimTable, _INITIAL_ADMIN_CLAIM_KEY)
        if row is None:
            raise RuntimeError("administrator bootstrap claim is missing")
        row.admin_token_id = token_id
        row.updated_at = utc_now()
        self.session.flush()

    def mark_published(self) -> None:
        row = self.session.get(IdentityBootstrapClaimTable, _INITIAL_ADMIN_CLAIM_KEY)
        if row is None:
            raise RuntimeError("administrator bootstrap claim is missing")
        published_at = utc_now()
        row.published_at = published_at
        row.updated_at = published_at
        self.session.flush()

    def administrator_ready(self) -> bool:
        claim = self.get()
        if claim is None or claim.published_at is None:
            return False
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        TokenTable.kind == TokenKind.Admin.value,
                        TokenTable.status == TokenStatus.Active.value,
                        TokenTable.disabled_by_admin.is_(False),
                    )
                )
            )
        )


@dataclass(frozen=True, slots=True)
class IdentityAdminRecoveryRequest:
    request_id: str
    workspace_id: str | None
    admin_token_id: str | None
    published_at: datetime | None


@dataclass(slots=True)
class IdentityAdminRecoveryRequestRepository:
    session: Session

    def get(self, request_id: str) -> IdentityAdminRecoveryRequest | None:
        row = self.session.get(IdentityAdminRecoveryRequestTable, request_id)
        if row is None:
            return None
        return IdentityAdminRecoveryRequest(
            request_id=row.request_id,
            workspace_id=row.workspace_id,
            admin_token_id=row.admin_token_id,
            published_at=row.published_at,
        )

    def create(
        self,
        *,
        request_id: str,
        workspace_id: str,
        admin_token_id: str,
    ) -> None:
        self.session.add(
            IdentityAdminRecoveryRequestTable(
                request_id=request_id,
                workspace_id=workspace_id,
                admin_token_id=admin_token_id,
            )
        )
        self.session.flush()

    def mark_published(self, request_id: str) -> None:
        row = self.session.get(IdentityAdminRecoveryRequestTable, request_id)
        if row is None:
            raise RuntimeError(f"administrator recovery request not found: {request_id}")
        published_at = utc_now()
        row.published_at = published_at
        row.updated_at = published_at
        self.session.flush()


class CredentialRecord(ContractModel):
    name: str
    token_prefix: str
    labels_key: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class WorkspaceAuditRecord(ContractModel):
    id: str
    workspace_id: str
    action: WorkspaceAuditAction
    actor_token_id: str | None = None
    actor_name: str
    target_type: WorkspaceAuditTarget
    target_id: str
    target_name: str
    summary: str
    previous_value: str | None = None
    new_value: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class WorkspaceAuditCursor:
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class WorkspaceAuditPage:
    records: tuple[WorkspaceAuditRecord, ...]
    next: WorkspaceAuditCursor | None = None


def normalize_username(value: str) -> str:
    """The one spelling of a username the database stores and looks up by."""
    return value.strip().lower()


@dataclass(slots=True)
class UserRepository:
    session: Session

    def create(
        self,
        *,
        username: str,
        password_hash: str,
        role: PlatformRole = PlatformRole.Member,
    ) -> UserRecord:
        now = utc_now()
        row = UserTable(
            username=normalize_username(username),
            password_hash=password_hash,
            role=role.value,
            status=UserStatus.Active.value,
            password_changed_at=now,
            payload={},
        )
        self.session.add(row)
        self.session.flush()
        record = user_record_from_table(row)
        row.payload = record.model_dump(mode="json")
        self.session.flush()
        return record

    def get(self, user_id: str) -> UserRecord | None:
        row = self.session.get(UserTable, user_id)
        return user_record_from_table(row) if row is not None else None

    def by_username(self, username: str) -> UserRecord | None:
        row = self.session.scalars(
            select(UserTable).where(UserTable.username == normalize_username(username))
        ).first()
        return user_record_from_table(row) if row is not None else None

    def list(self) -> list[UserRecord]:
        rows = self.session.scalars(
            select(UserTable).order_by(UserTable.created_at.desc(), UserTable.id.asc())
        )
        return [user_record_from_table(row) for row in rows]

    def set_password(self, user_id: str, *, password_hash: str) -> UserRecord:
        return self._update(user_id, password_hash=password_hash, password_changed_at=utc_now())

    def set_status(self, user_id: str, *, status: UserStatus) -> UserRecord:
        return self._update(user_id, status=status.value)

    def set_role(self, user_id: str, *, role: PlatformRole) -> UserRecord:
        return self._update(user_id, role=role.value)

    def delete(self, user_id: str) -> bool:
        result = self.session.execute(delete(UserTable).where(UserTable.id == user_id))
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount > 0

    def _update(self, user_id: str, **columns: object) -> UserRecord:
        row = self.session.get(UserTable, user_id)
        if row is None:
            raise NotFoundError(f"user not found: {user_id}")
        for column, value in columns.items():
            setattr(row, column, value)
        row.updated_at = utc_now()
        self.session.flush()
        record = user_record_from_table(row)
        row.payload = record.model_dump(mode="json")
        self.session.flush()
        return record


@dataclass(slots=True)
class WorkspaceMemberRepository:
    session: Session

    def add(
        self,
        *,
        workspace_id: str,
        user_id: str,
        role: WorkspaceRole = WorkspaceRole.Member,
    ) -> WorkspaceMemberRecord:
        # Two different constraints can refuse this insert, and they mean different
        # things to whoever is asking. Reading the existing rows first is what lets
        # the refusal name the actual reason; the indexes still decide under a race.
        if self.membership(workspace_id=workspace_id, user_id=user_id) is not None:
            raise ConflictError(f"user is already a member of this workspace: {user_id}")
        if role is WorkspaceRole.Owner and self.owner(workspace_id) is not None:
            raise ConflictError(
                f"workspace already has an owner; transfer ownership instead: {workspace_id}"
            )
        row = WorkspaceMemberTable(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role.value,
            payload={},
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                f"workspace membership conflicts with an existing row: {workspace_id}"
            ) from exc
        record = workspace_member_record_from_table(row)
        row.payload = record.model_dump(mode="json")
        self.session.flush()
        return record

    def membership(self, *, workspace_id: str, user_id: str) -> WorkspaceMemberRecord | None:
        """The single row that decides whether this person reaches this workspace."""
        row = self.session.scalars(
            select(WorkspaceMemberTable).where(
                WorkspaceMemberTable.workspace_id == workspace_id,
                WorkspaceMemberTable.user_id == user_id,
            )
        ).first()
        return workspace_member_record_from_table(row) if row is not None else None

    def owner(self, workspace_id: str) -> WorkspaceMemberRecord | None:
        """Whose account backs this workspace—the compute and domains resolve through it."""
        row = self.session.scalars(
            select(WorkspaceMemberTable).where(
                WorkspaceMemberTable.workspace_id == workspace_id,
                WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
            )
        ).first()
        return workspace_member_record_from_table(row) if row is not None else None

    def ensure_owner(self, *, workspace_id: str, user_id: str) -> WorkspaceMemberRecord:
        """The workspace's single owner, written for `user_id` when it has none.

        A workspace whose owner row is missing is unreachable by every person and
        resolves to no compute account, so the row is written with the workspace
        rather than left to whoever remembers. A workspace that already has an owner
        keeps it: adoption must not transfer the account the workspace resolves
        through, and the partial unique index refuses a second one regardless.
        """
        existing = self.owner(workspace_id)
        if existing is not None:
            return existing
        return self.add(workspace_id=workspace_id, user_id=user_id, role=WorkspaceRole.Owner)

    def owner_user_id(self, workspace_id: str) -> str:
        owner = self.owner(workspace_id)
        if owner is None:
            raise NotFoundError(f"workspace has no owner: {workspace_id}")
        return owner.user_id

    def for_workspace(self, workspace_id: str) -> list[WorkspaceMemberRecord]:
        rows = self.session.scalars(
            select(WorkspaceMemberTable)
            .where(WorkspaceMemberTable.workspace_id == workspace_id)
            .order_by(WorkspaceMemberTable.created_at.asc())
        )
        return [workspace_member_record_from_table(row) for row in rows]

    def owned_workspace_ids(self, user_id: str) -> list[str]:
        """Workspaces this account backs—the ones its compute and domains apply to."""
        rows = self.session.scalars(
            select(WorkspaceMemberTable.workspace_id)
            .where(
                WorkspaceMemberTable.user_id == user_id,
                WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
            )
            .order_by(WorkspaceMemberTable.created_at.asc())
        )
        return [str(row) for row in rows]

    def for_user(self, user_id: str) -> list[WorkspaceMemberRecord]:
        rows = self.session.scalars(
            select(WorkspaceMemberTable)
            .where(WorkspaceMemberTable.user_id == user_id)
            .order_by(WorkspaceMemberTable.created_at.asc())
        )
        return [workspace_member_record_from_table(row) for row in rows]

    def workspaces_for_user(self, user_id: str) -> list[WorkspaceRecord]:
        """Active workspaces this person reaches, resolved in one query.

        A deleting or deleted workspace is excluded here rather than by the caller:
        every consumer wants the set a person may actually act on.
        """
        rows = self.session.scalars(
            select(WorkspaceTable)
            .join(WorkspaceMemberTable, WorkspaceMemberTable.workspace_id == WorkspaceTable.id)
            .where(WorkspaceMemberTable.user_id == user_id)
            .order_by(WorkspaceTable.created_at.asc())
        )
        # Status lives in the workspace payload rather than a column, so the filter
        # happens after mapping, the same way WorkspaceRepository.list does it.
        workspaces = [WorkspaceRecord.model_validate(row.payload) for row in rows]
        return [item for item in workspaces if item.status is WorkspaceStatus.Active]

    def set_role(self, *, workspace_id: str, user_id: str, role: WorkspaceRole) -> None:
        self.session.execute(
            update(WorkspaceMemberTable)
            .where(
                WorkspaceMemberTable.workspace_id == workspace_id,
                WorkspaceMemberTable.user_id == user_id,
            )
            .values(role=role.value, updated_at=utc_now())
        )
        self.session.flush()

    def remove(self, *, workspace_id: str, user_id: str) -> bool:
        result = self.session.execute(
            delete(WorkspaceMemberTable).where(
                WorkspaceMemberTable.workspace_id == workspace_id,
                WorkspaceMemberTable.user_id == user_id,
            )
        )
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount > 0


@dataclass(slots=True)
class WorkspaceRepository:
    session: Session

    @property
    def records(self) -> GlobalTableRepository[WorkspaceRecord]:
        return GlobalTableRepository(
            self.session,
            TableRepositoryConfig(WorkspaceTable, WorkspaceRecord),
        )

    def create(self, *, name: str, signing_key: str | None = None) -> WorkspaceRecord:
        payload: dict[str, JsonValue] = {
            "name": name,
            "signing_key": signing_key or new_signing_key(),
            "status": WorkspaceStatus.Active.value,
        }
        return self.records.create(payload, name=name)

    def upsert(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        return self.records.upsert(workspace, name=workspace.name)

    def get(self, workspace_id: str) -> WorkspaceRecord | None:
        return self.records.get(workspace_id)

    def by_name(self, name: str) -> WorkspaceRecord | None:
        matches = self.records.list(name=name)
        return matches[0] if matches else None

    def resolve_for_deletion(self, workspace_id_or_name: str) -> WorkspaceRecord | None:
        """System lookup that retains Deleting and Deleted tombstones."""
        return self.get(workspace_id_or_name) or self.by_name(workspace_id_or_name)

    def lock_for_deletion(self, workspace_id: str) -> WorkspaceRecord:
        """Exclusively fence admission before changing workspace lifecycle state."""
        row = self.session.scalars(
            select(WorkspaceTable)
            .where(WorkspaceTable.id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        return _workspace_record(row, workspace_id)

    def list(self) -> list[WorkspaceRecord]:
        return self.records.list()

    def lock_active_owner(self, workspace_id: str) -> WorkspaceRecord:
        """Fence a tenant-owned write against irreversible workspace deletion.

        PostgreSQL key-share locks are mutually compatible, so independent
        reconcilers can continue writing state for the same active workspace.
        They conflict with the deletion transaction's row lock: a writer that
        arrives first commits before the purge, while a writer that arrives
        after deletion observes the tombstone and cannot recreate owned state.
        """
        row = self.session.scalars(
            select(WorkspaceTable)
            .where(WorkspaceTable.id == workspace_id)
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        ).first()
        workspace = _workspace_record(row, workspace_id)
        if workspace.status is not WorkspaceStatus.Active:
            raise NotFoundError(f"workspace not found: {workspace_id}")
        return workspace

    def lock_object_write_completion_owner(self, workspace_id: str) -> WorkspaceRecord:
        """Fence completion of a write admitted before deletion began."""
        row = self.session.scalars(
            select(WorkspaceTable)
            .where(WorkspaceTable.id == workspace_id)
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        ).first()
        workspace = _workspace_record(row, workspace_id)
        if workspace.status not in {WorkspaceStatus.Active, WorkspaceStatus.Deleting}:
            raise NotFoundError(f"workspace not found: {workspace_id}")
        return workspace

    def deletion_blockers(self, workspace_id: str) -> tuple[str, ...]:
        """Return every live customer resource table still owned by the workspace."""
        blockers: list[str] = []
        for table in sorted(WorkspaceTable.metadata.tables.values(), key=lambda item: item.name):
            workspace_column = table.columns.get("workspace_id")
            if table.name in _workspace_purge_excluded_tables() or workspace_column is None:
                continue
            if self.session.scalar(select(exists().where(workspace_column == workspace_id))):
                blockers.append(table.name)
        return tuple(blockers)

    def owned_resource_ids(self, workspace_id: str) -> tuple[str, ...]:
        resource_ids: set[str] = set()
        for table in WorkspaceTable.metadata.tables.values():
            workspace_column = table.columns.get("workspace_id")
            id_column = table.columns.get("id")
            if (
                table.name in _workspace_purge_excluded_tables()
                or workspace_column is None
                or id_column is None
            ):
                continue
            resource_ids.update(
                _STRINGS_ADAPTER.validate_python(
                    self.session.scalars(
                        select(id_column).where(workspace_column == workspace_id)
                    ).all()
                )
            )
        return tuple(sorted(resource_ids))

    def purge_owned_records(self, workspace_id: str) -> dict[str, int]:
        row = self.session.scalars(
            select(WorkspaceTable)
            .where(WorkspaceTable.id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        workspace = _workspace_record(row, workspace_id)
        if workspace.status not in {WorkspaceStatus.Deleting, WorkspaceStatus.Deleted}:
            raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")

        deleted: dict[str, int] = {}
        tables = sorted(
            WorkspaceTable.metadata.tables.values(),
            key=lambda table: table.name,
            reverse=True,
        )
        for table in tables:
            workspace_column = table.columns.get("workspace_id")
            if table.name in _workspace_purge_excluded_tables() or workspace_column is None:
                continue
            result = self.session.execute(delete(table).where(workspace_column == workspace_id))
            count = int(result.rowcount) if isinstance(result, CursorResult) else 0
            if count:
                deleted[table.name] = count
        self.session.flush()
        return deleted

    def tombstone(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        if workspace.status is not WorkspaceStatus.Deleting:
            raise ConflictError(f"workspace finalization requires deleting state: {workspace.id}")
        workspace.status = WorkspaceStatus.Deleted
        workspace.signing_key = ""
        workspace.signing_key_prefix = None
        workspace.primary_token_id = None
        workspace.concurrency_limit_id = None
        workspace.storage = WorkspaceStorageConfig()
        workspace.labels.clear()
        workspace.metadata.clear()
        workspace.updated_at = utc_now()
        return self.upsert(workspace)

    def mark_deleting(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        if workspace.status is WorkspaceStatus.Deleted:
            return workspace
        if workspace.status not in {WorkspaceStatus.Active, WorkspaceStatus.Deleting}:
            raise ConflictError(f"workspace is not active: {workspace.id}")
        workspace.status = WorkspaceStatus.Deleting
        workspace.signing_key = ""
        workspace.signing_key_prefix = None
        workspace.primary_token_id = None
        workspace.updated_at = utc_now()
        return self.upsert(workspace)

    def delete_identity_records(self, workspace_id: str) -> None:
        # Device authorizations are not here: a pending CLI login belongs to the person
        # who started it and reaches every workspace they hold, so deleting one
        # workspace must not cancel it.
        for table in (
            TokenTable,
            ConcurrencyLimitTable,
            WorkspaceStorageTable,
        ):
            self.session.execute(delete(table).where(table.workspace_id == workspace_id))
        self.session.execute(
            delete(WorkspaceMemberTable).where(WorkspaceMemberTable.workspace_id == workspace_id)
        )
        self.session.flush()

    def ensure_named(self, name: str, *, signing_key: str | None = None) -> WorkspaceRecord:
        current = self.by_name(name)
        if current is not None:
            if current.status is not WorkspaceStatus.Active:
                raise ConflictError(f"workspace name is retained after deletion: {name}")
            return current
        return self.create(name=name, signing_key=signing_key)


def _workspace_purge_excluded_tables() -> set[str]:
    return {
        _mapped_table_name(EventTable),
        _mapped_table_name(WorkspaceAuditEventTable),
        _mapped_table_name(WorkspaceTable),
        _mapped_table_name(WorkspaceStorageTable),
        _mapped_table_name(TokenTable),
        _mapped_table_name(DeviceAuthorizationTable),
        _mapped_table_name(ConcurrencyLimitTable),
        _mapped_table_name(SourceCacheCleanupTargetTable),
        _mapped_table_name(WorkerCacheGenerationTable),
        _mapped_table_name(UsageRecordTable),
        _mapped_table_name(UsageBillingWindowTable),
        _mapped_table_name(UsageBillingContributionTable),
    }


def _workspace_record(
    row: WorkspaceTable | None,
    workspace_id: str,
) -> WorkspaceRecord:
    if row is None:
        raise NotFoundError(f"workspace not found: {workspace_id}")
    return WorkspaceRecord.model_validate(row.payload)


def _mapped_table_name(model: type[DatabaseBase]) -> str:
    mapped_table = class_mapper(model).local_table
    for table_name, table in model.metadata.tables.items():
        if table is mapped_table:
            return table_name
    raise ValueError(f"{model.__name__} has no table in its declarative metadata")


@dataclass(slots=True)
class WorkspaceStorageRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[WorkspaceStorageRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(WorkspaceStorageTable, WorkspaceStorageRecord),
        )

    def upsert(self, record: WorkspaceStorageRecord) -> WorkspaceStorageRecord:
        return self.records.upsert(record, workspace_id=record.workspace_id)

    def get(self, storage_id: str, *, workspace_id: str) -> WorkspaceStorageRecord | None:
        return self.records.get(storage_id, workspace_id=workspace_id)

    def list(self, *, workspace_id: str) -> list[WorkspaceStorageRecord]:
        return self.records.list(workspace_id=workspace_id)


@dataclass(slots=True)
class WorkspaceAuditRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[WorkspaceAuditRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(WorkspaceAuditEventTable, WorkspaceAuditRecord),
        )

    def append(
        self,
        *,
        workspace_id: str,
        action: WorkspaceAuditAction,
        actor: AuthTokenRecord,
        target_type: WorkspaceAuditTarget,
        target_id: str,
        target_name: str,
        summary: str,
        previous_value: str | None = None,
        new_value: str | None = None,
    ) -> WorkspaceAuditRecord:
        return self.records.create(
            {
                "workspace_id": workspace_id,
                "action": action.value,
                "actor_token_id": actor.id,
                "actor_name": actor.name,
                "target_type": target_type.value,
                "target_id": target_id,
                "target_name": target_name,
                "summary": summary,
                "previous_value": previous_value,
                "new_value": new_value,
            },
            workspace_id=workspace_id,
        )

    def append_workspace_deleted(
        self,
        *,
        workspace_id: str,
        actor: AuthTokenRecord,
        target_name: str,
    ) -> WorkspaceAuditRecord:
        """Append the sole deletion audit while its workspace is fenced Deleting."""
        workspace = WorkspaceRepository(self.session).lock_for_deletion(workspace_id)
        if workspace.status is not WorkspaceStatus.Deleted:
            raise ConflictError(f"workspace deletion audit requires deleted state: {workspace_id}")
        record = WorkspaceAuditRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            action=WorkspaceAuditAction.WorkspaceDeleted,
            actor_token_id=actor.id,
            actor_name=actor.name,
            target_type=WorkspaceAuditTarget.Workspace,
            target_id=workspace_id,
            target_name=target_name,
            summary=f"Deleted workspace {target_name}",
            previous_value=WorkspaceStatus.Active.value,
            new_value=WorkspaceStatus.Deleted.value,
        )
        self.session.add(
            WorkspaceAuditEventTable(
                id=record.id,
                workspace_id=workspace_id,
                actor_token_id=actor.id,
                action=record.action.value,
                target_type=record.target_type.value,
                target_id=record.target_id,
                payload=record.model_dump(mode="json"),
                created_at=record.created_at,
                updated_at=record.created_at,
            )
        )
        self.session.flush()
        return record

    def page(
        self,
        *,
        workspace_id: str,
        limit: int,
        cursor: WorkspaceAuditCursor | None = None,
    ) -> WorkspaceAuditPage:
        statement = select(WorkspaceAuditEventTable).where(
            WorkspaceAuditEventTable.workspace_id == workspace_id
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    WorkspaceAuditEventTable.created_at < cursor.created_at,
                    and_(
                        WorkspaceAuditEventTable.created_at == cursor.created_at,
                        WorkspaceAuditEventTable.id > cursor.id,
                    ),
                )
            )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    WorkspaceAuditEventTable.created_at.desc(),
                    WorkspaceAuditEventTable.id.asc(),
                ).limit(limit + 1)
            )
        )
        page_rows = rows[:limit]
        records = tuple(WorkspaceAuditRecord.model_validate(row.payload) for row in page_rows)
        next_cursor = None
        if len(rows) > limit and page_rows:
            last = page_rows[-1]
            next_cursor = WorkspaceAuditCursor(created_at=last.created_at, id=str(last.id))
        return WorkspaceAuditPage(records=records, next=next_cursor)


@dataclass(slots=True)
class ConcurrencyLimitRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[ConcurrencyLimitRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(ConcurrencyLimitTable, ConcurrencyLimitRecord),
        )

    def upsert(self, record: ConcurrencyLimitRecord) -> ConcurrencyLimitRecord:
        return self.records.upsert(
            record,
            workspace_id=record.workspace_id,
            name=record.name,
            status=record.resource_type,
        )

    def get(self, limit_id: str, *, workspace_id: str) -> ConcurrencyLimitRecord | None:
        return self.records.get(limit_id, workspace_id=workspace_id)

    def list(self, *, workspace_id: str) -> list[ConcurrencyLimitRecord]:
        return self.records.list(workspace_id=workspace_id)


@dataclass(slots=True)
class CredentialRepository:
    session: Session

    @property
    def records(self) -> WorkspaceTableRepository[CredentialRecord]:
        return WorkspaceTableRepository(
            self.session,
            TableRepositoryConfig(CredentialTable, CredentialRecord, key_field="name"),
        )

    def upsert(self, record: CredentialRecord, *, workspace_id: str) -> CredentialRecord:
        return self.records.upsert(
            record,
            key=record.name,
            workspace_id=workspace_id,
            name=record.name,
        )

    def list(self, *, workspace_id: str) -> list[CredentialRecord]:
        return self.records.list(workspace_id=workspace_id)


@dataclass(slots=True)
class TokenRepository:
    session: Session

    def create(
        self,
        *,
        name: str,
        token_hash: str,
        prefix: str,
        kind: TokenKind,
        user_id: str = "",
        workspace_id: str = "",
        worker_id: str = "",
        scopes: list[str] | None = None,
        reusable: bool = True,
        expires_at: datetime | None = None,
    ) -> AuthTokenRecord:
        if bool(user_id) == bool(workspace_id):
            msg = "a token names exactly one principal: a user or a workspace"
            raise ValueError(msg)
        row = TokenTable(
            name=name,
            token_hash=token_hash,
            prefix=prefix,
            kind=kind.value,
            user_id=user_id or None,
            workspace_id=workspace_id or None,
            worker_id=worker_id,
            status=TokenStatus.Active.value,
            scopes=list(scopes if scopes is not None else ["*"]),
            reusable=reusable,
            disabled_by_admin=False,
            expires_at=expires_at,
        )
        self.session.add(row)
        self.session.flush()
        return auth_token_record_from_table(row)

    def list_for_user(self, user_id: str) -> list[AuthTokenRecord]:
        rows = self.session.scalars(
            select(TokenTable)
            .where(TokenTable.user_id == user_id)
            .order_by(TokenTable.created_at.desc(), TokenTable.id.asc())
        )
        return [auth_token_record_from_table(row) for row in rows]

    def get_for_user(self, token_id: str, *, user_id: str) -> AuthTokenRecord | None:
        row = self.session.scalars(
            select(TokenTable).where(
                TokenTable.id == token_id,
                TokenTable.user_id == user_id,
            )
        ).first()
        return auth_token_record_from_table(row) if row is not None else None

    def revoke_user_sessions(self, user_id: str, *, now: datetime) -> int:
        """End every live session a person holds, which is what a password change means."""
        result = self.session.execute(
            update(TokenTable)
            .where(
                TokenTable.user_id == user_id,
                TokenTable.kind == TokenKind.Session.value,
                TokenTable.status == TokenStatus.Active.value,
            )
            .values(
                status=TokenStatus.Revoked.value,
                revoked_at=now,
                updated_at=now,
            )
        )
        self.session.flush()
        return int(result.rowcount) if isinstance(result, CursorResult) else 0

    def list_by_prefix(self, prefix: str) -> list[AuthTokenRecord]:
        """Authentication lookup by opaque token prefix; runs before any workspace exists."""
        statement = select(TokenTable).where(TokenTable.prefix == prefix).order_by(TokenTable.id)
        return [auth_token_record_from_table(row) for row in self.session.scalars(statement)]

    def get(self, token_id: str, *, workspace_id: str) -> AuthTokenRecord | None:
        row = self.session.scalars(
            select(TokenTable).where(
                TokenTable.id == token_id,
                TokenTable.workspace_id == workspace_id,
            )
        ).first()
        return auth_token_record_from_table(row) if row is not None else None

    def get_across_workspaces(self, token_id: str) -> AuthTokenRecord | None:
        """Admin/system lookup; callers must hold admin or system authority."""
        row = self.session.get(TokenTable, token_id)
        return auth_token_record_from_table(row) if row is not None else None

    def list(self, *, workspace_id: str) -> list[AuthTokenRecord]:
        rows = self.session.scalars(
            select(TokenTable)
            .where(TokenTable.workspace_id == workspace_id)
            .order_by(TokenTable.created_at.desc(), TokenTable.id.asc())
        )
        return [auth_token_record_from_table(row) for row in rows]

    def list_across_workspaces(self) -> list[AuthTokenRecord]:
        """Admin/system listing across every workspace."""
        rows = self.session.scalars(
            select(TokenTable).order_by(TokenTable.created_at.desc(), TokenTable.id.asc())
        )
        return [auth_token_record_from_table(row) for row in rows]

    def list_owned_credentials(
        self,
        *,
        workspace_id: str,
        name: str,
        kind: TokenKind,
    ) -> list[AuthTokenRecord]:
        statement = select(TokenTable).where(
            TokenTable.workspace_id == workspace_id,
            TokenTable.name == name,
            TokenTable.kind == kind.value,
        )
        return [auth_token_record_from_table(row) for row in self.session.scalars(statement)]

    def delete(self, token_id: str, *, workspace_id: str) -> bool:
        result = self.session.execute(
            delete(TokenTable).where(
                TokenTable.id == token_id,
                TokenTable.workspace_id == workspace_id,
            )
        )
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount > 0

    def delete_across_workspaces(self, token_id: str) -> bool:
        """Admin/system deletion regardless of owning workspace."""
        result = self.session.execute(delete(TokenTable).where(TokenTable.id == token_id))
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount > 0

    def revoke_workspace_for_deletion(self, workspace_id: str, *, now: datetime) -> int:
        """Revoke every target credential inside the atomic deletion-begin transaction."""
        result = self.session.execute(
            update(TokenTable)
            .where(
                TokenTable.workspace_id == workspace_id,
                TokenTable.status == TokenStatus.Active.value,
            )
            .values(
                status=TokenStatus.Revoked.value,
                revoked_at=now,
                updated_at=now,
            )
        )
        self.session.flush()
        return int(result.rowcount) if isinstance(result, CursorResult) else 0

    def is_consumed(self, token_id: str, *, workspace_id: str | None = None) -> bool:
        statement = select(TokenTable.consumed_at).where(TokenTable.id == token_id)
        if workspace_id is not None:
            statement = statement.where(TokenTable.workspace_id == workspace_id)
        return self.session.scalar(statement) is not None

    def mark_reusable_used(self, token_id: str, *, now: datetime) -> AuthTokenRecord | None:
        """Record use only while the reusable credential remains valid."""
        row = self.session.scalars(
            update(TokenTable)
            .where(
                TokenTable.id == token_id,
                TokenTable.reusable.is_(True),
                TokenTable.status == TokenStatus.Active.value,
                TokenTable.disabled_by_admin.is_(False),
                or_(TokenTable.expires_at.is_(None), TokenTable.expires_at > now),
            )
            .values(last_used_at=now, updated_at=now)
            .returning(TokenTable)
        ).first()
        self.session.flush()
        return auth_token_record_from_table(row) if row is not None else None

    def consume_non_reusable(self, token_id: str, *, now: datetime) -> bool:
        """Atomically grant the sole claim on an active non-reusable credential."""
        result = self.session.execute(
            update(TokenTable)
            .where(
                TokenTable.id == token_id,
                TokenTable.reusable.is_(False),
                TokenTable.status == TokenStatus.Active.value,
                TokenTable.disabled_by_admin.is_(False),
                TokenTable.consumed_at.is_(None),
                or_(TokenTable.expires_at.is_(None), TokenTable.expires_at > now),
            )
            .values(
                consumed_at=now,
                last_used_at=now,
                status=TokenStatus.Revoked.value,
                revoked_at=now,
                updated_at=now,
            )
        )
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount == 1

    def revoke_if_expired(self, token_id: str, *, now: datetime) -> bool:
        result = self.session.execute(
            update(TokenTable)
            .where(
                TokenTable.id == token_id,
                TokenTable.status == TokenStatus.Active.value,
                TokenTable.expires_at.is_not(None),
                TokenTable.expires_at <= now,
            )
            .values(
                status=TokenStatus.Revoked.value,
                revoked_at=func.coalesce(TokenTable.revoked_at, now),
                updated_at=now,
            )
        )
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount == 1

    def activate(
        self,
        token_id: str,
        *,
        workspace_id: str,
        now: datetime,
    ) -> AuthTokenRecord | None:
        """Reactivate only a non-consumed credential that has not expired."""
        row = self.session.scalars(
            update(TokenTable)
            .where(
                TokenTable.id == token_id,
                TokenTable.workspace_id == workspace_id,
                TokenTable.status == TokenStatus.Revoked.value,
                TokenTable.consumed_at.is_(None),
                or_(TokenTable.expires_at.is_(None), TokenTable.expires_at > now),
            )
            .values(
                status=TokenStatus.Active.value,
                revoked_at=None,
                updated_at=now,
            )
            .returning(TokenTable)
        ).first()
        self.session.flush()
        return auth_token_record_from_table(row) if row is not None else None

    def revoke(
        self,
        token_id: str,
        *,
        workspace_id: str,
        now: datetime,
    ) -> AuthTokenRecord | None:
        row = self.session.scalars(
            update(TokenTable)
            .where(
                TokenTable.id == token_id,
                TokenTable.workspace_id == workspace_id,
            )
            .values(
                status=TokenStatus.Revoked.value,
                revoked_at=func.coalesce(TokenTable.revoked_at, now),
                updated_at=now,
            )
            .returning(TokenTable)
        ).first()
        self.session.flush()
        return auth_token_record_from_table(row) if row is not None else None

    def revoke_across_workspaces(
        self,
        token_id: str,
        *,
        now: datetime,
    ) -> AuthTokenRecord | None:
        row = self.session.scalars(
            update(TokenTable)
            .where(TokenTable.id == token_id)
            .values(
                status=TokenStatus.Revoked.value,
                revoked_at=now,
                updated_at=now,
            )
            .returning(TokenTable)
        ).first()
        self.session.flush()
        return auth_token_record_from_table(row) if row is not None else None

    def set_admin_disabled(
        self,
        token_id: str,
        *,
        workspace_id: str,
        disabled: bool,
        now: datetime,
    ) -> AuthTokenRecord | None:
        row = self.session.scalars(
            update(TokenTable)
            .where(
                TokenTable.id == token_id,
                TokenTable.workspace_id == workspace_id,
            )
            .values(disabled_by_admin=disabled, updated_at=now)
            .returning(TokenTable)
        ).first()
        self.session.flush()
        return auth_token_record_from_table(row) if row is not None else None

    def prune_expired(self, *, now: datetime, kinds: Collection[TokenKind]) -> int:
        """Delete tokens of the given kinds whose expiry has passed."""
        if not kinds:
            return 0
        result = self.session.execute(
            delete(TokenTable).where(
                TokenTable.kind.in_([kind.value for kind in kinds]),
                TokenTable.expires_at.is_not(None),
                TokenTable.expires_at <= now,
            )
        )
        self.session.flush()
        return int(result.rowcount) if isinstance(result, CursorResult) else 0


@dataclass(slots=True)
class DeviceAuthorizationRepository:
    session: Session

    def create_pending(
        self,
        *,
        device_code_hash: str,
        user_code: str,
        client_name: str,
        expires_at: datetime,
    ) -> _DeviceAuthorizationRecord | None:
        """Insert one pending request, returning ``None`` on a user-code collision.

        The unique constraint is the allocation authority. A savepoint contains
        a losing insert so the service can retry without poisoning its outer
        transaction. Other integrity failures remain fatal.
        """
        row = DeviceAuthorizationTable(
            device_code_hash=device_code_hash,
            user_code=user_code,
            client_name=client_name,
            status=DeviceAuthorizationStatus.Pending.value,
            user_id=None,
            expires_at=expires_at,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            existing_id = self.session.scalar(
                select(DeviceAuthorizationTable.id).where(
                    DeviceAuthorizationTable.user_code == user_code
                )
            )
            if existing_id is not None:
                return None
            raise
        return device_authorization_record_from_table(row)

    def decide_pending(
        self,
        record: _DeviceAuthorizationRecord,
        *,
        status: DeviceAuthorizationStatus,
        user_id: str | None,
        decided_at: datetime,
    ) -> _DeviceAuthorizationRecord | None:
        if status not in {
            DeviceAuthorizationStatus.Approved,
            DeviceAuthorizationStatus.Denied,
        }:
            msg = "a device authorization decision must be approved or denied"
            raise ValueError(msg)
        if (status is DeviceAuthorizationStatus.Approved) != (user_id is not None):
            msg = "only approved device authorizations bind a user"
            raise ValueError(msg)
        statement = (
            update(DeviceAuthorizationTable)
            .where(
                DeviceAuthorizationTable.id == record.id,
                DeviceAuthorizationTable.status == DeviceAuthorizationStatus.Pending.value,
                DeviceAuthorizationTable.consumed_at.is_(None),
                DeviceAuthorizationTable.expires_at > decided_at,
            )
            .values(
                status=status.value,
                user_id=user_id,
                updated_at=decided_at,
            )
            .returning(DeviceAuthorizationTable)
            .execution_options(synchronize_session=False)
        )
        row = self.session.scalars(statement).first()
        self.session.flush()
        return device_authorization_record_from_table(row) if row is not None else None

    def consume_decided(
        self,
        record: _DeviceAuthorizationRecord,
        *,
        consumed_at: datetime,
    ) -> _DeviceAuthorizationRecord | None:
        statement = (
            update(DeviceAuthorizationTable)
            .where(
                DeviceAuthorizationTable.id == record.id,
                DeviceAuthorizationTable.status.in_(
                    (
                        DeviceAuthorizationStatus.Approved.value,
                        DeviceAuthorizationStatus.Denied.value,
                    )
                ),
                DeviceAuthorizationTable.consumed_at.is_(None),
                DeviceAuthorizationTable.expires_at > consumed_at,
            )
            .values(
                consumed_at=consumed_at,
                updated_at=consumed_at,
            )
            .returning(DeviceAuthorizationTable)
            .execution_options(synchronize_session=False)
        )
        row = self.session.scalars(statement).first()
        self.session.flush()
        return device_authorization_record_from_table(row) if row is not None else None

    def delete_if_expired(
        self,
        record: _DeviceAuthorizationRecord,
        *,
        expired_at: datetime,
    ) -> bool:
        statement = (
            delete(DeviceAuthorizationTable)
            .where(
                DeviceAuthorizationTable.id == record.id,
                DeviceAuthorizationTable.consumed_at.is_(None),
                DeviceAuthorizationTable.expires_at <= expired_at,
            )
            .returning(DeviceAuthorizationTable.id)
            .execution_options(synchronize_session=False)
        )
        deleted_id = self.session.scalar(statement)
        self.session.flush()
        return deleted_id is not None

    def by_device_code_hash(self, device_code_hash: str) -> _DeviceAuthorizationRecord | None:
        statement = select(DeviceAuthorizationTable).where(
            DeviceAuthorizationTable.device_code_hash == device_code_hash
        )
        row = self.session.scalars(statement).first()
        return device_authorization_record_from_table(row) if row is not None else None

    def by_user_code(self, user_code: str) -> _DeviceAuthorizationRecord | None:
        statement = select(DeviceAuthorizationTable).where(
            DeviceAuthorizationTable.user_code == user_code
        )
        row = self.session.scalars(statement).first()
        return device_authorization_record_from_table(row) if row is not None else None

    def prune_expired(self, *, now: datetime) -> int:
        result = self.session.execute(
            delete(DeviceAuthorizationTable).where(DeviceAuthorizationTable.expires_at <= now)
        )
        self.session.flush()
        return int(result.rowcount) if isinstance(result, CursorResult) else 0


@dataclass(slots=True)
class SecretRepository:
    session: Session

    def create(self, name: str, ciphertext: str, *, workspace_id: str) -> SecretStorageRecord:
        row = SecretTable(workspace_id=workspace_id, name=name, ciphertext=ciphertext)
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            if self.get(name, workspace_id=workspace_id) is not None:
                raise ConflictError(f"secret already exists: {name}") from None
            raise
        return secret_storage_record_from_table(row)

    def set(
        self,
        name: str,
        ciphertext: str,
        *,
        workspace_id: str,
    ) -> tuple[SecretStorageRecord, bool]:
        now = utc_now()
        updated = self._update_row(
            name,
            ciphertext,
            workspace_id=workspace_id,
            updated_at=now,
        )
        if updated is not None:
            return secret_storage_record_from_table(updated), False

        row = SecretTable(workspace_id=workspace_id, name=name, ciphertext=ciphertext)
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            updated = self._update_row(
                name,
                ciphertext,
                workspace_id=workspace_id,
                updated_at=utc_now(),
            )
            if updated is None:
                raise
            return secret_storage_record_from_table(updated), False
        return secret_storage_record_from_table(row), True

    def update(self, name: str, ciphertext: str, *, workspace_id: str) -> SecretStorageRecord:
        row = self._update_row(
            name,
            ciphertext,
            workspace_id=workspace_id,
            updated_at=utc_now(),
        )
        if row is None:
            raise NotFoundError(f"secret not found: {name}")
        return secret_storage_record_from_table(row)

    def get(self, name: str, *, workspace_id: str) -> SecretStorageRecord | None:
        row = self.session.scalars(
            select(SecretTable).where(
                SecretTable.workspace_id == workspace_id,
                SecretTable.name == name,
            )
        ).first()
        return secret_storage_record_from_table(row) if row is not None else None

    def list(self, *, workspace_id: str) -> list[SecretStorageRecord]:
        rows = self.session.scalars(
            select(SecretTable)
            .where(SecretTable.workspace_id == workspace_id)
            .order_by(SecretTable.name, SecretTable.id)
        ).all()
        return [secret_storage_record_from_table(row) for row in rows]

    def delete(self, name: str, *, workspace_id: str) -> SecretStorageRecord:
        row = self.session.scalars(
            delete(SecretTable)
            .where(
                SecretTable.workspace_id == workspace_id,
                SecretTable.name == name,
            )
            .returning(SecretTable)
        ).first()
        self.session.flush()
        if row is None:
            raise NotFoundError(f"secret not found: {name}")
        return secret_storage_record_from_table(row)

    def _update_row(
        self,
        name: str,
        ciphertext: str,
        *,
        workspace_id: str,
        updated_at: datetime,
    ) -> SecretTable | None:
        row = self.session.scalars(
            update(SecretTable)
            .where(
                SecretTable.workspace_id == workspace_id,
                SecretTable.name == name,
            )
            .values(
                ciphertext=ciphertext,
                updated_at=case(
                    (SecretTable.updated_at > updated_at, SecretTable.updated_at),
                    else_=updated_at,
                ),
            )
            .returning(SecretTable)
            .execution_options(synchronize_session=False)
        ).first()
        self.session.flush()
        return row
