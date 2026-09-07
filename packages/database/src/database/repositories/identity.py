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
    user_identity_record_from_table,
    user_record_from_table,
    workspace_invitation_record_from_table,
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
from database.tables.billing_ledger import (
    BillingLedgerSegmentTable,
    ContainerBillingShapeTable,
)
from database.tables.billing_outbox import BillingMeterOutboxTable
from database.tables.execution import EventTable
from database.tables.identity import (
    ConcurrencyLimitTable,
    DeviceAuthorizationTable,
    IdentityAdminRecoveryRequestTable,
    IdentityBootstrapClaimTable,
    SecretTable,
    TokenTable,
    UserIdentityTable,
    UserTable,
    WorkspaceAuditEventTable,
    WorkspaceInvitationTable,
    WorkspaceMemberTable,
    WorkspaceStorageTable,
    WorkspaceTable,
)
from database.tables.observability import (
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
    DeviceAuthorizationStatus,
    IdentityProvider,
    PlatformRole,
    TokenKind,
    TokenStatus,
    UserIdentityRecord,
    UserRecord,
    UserStatus,
    WorkspaceInvitationRecord,
    WorkspaceInvitationRole,
    WorkspaceMemberRecord,
    WorkspaceRecord,
    WorkspaceRole,
    WorkspaceStatus,
    WorkspaceStorageConfig,
)
from shared.timestamps import utc_now
from sqlalchemy import and_, case, delete, exists, func, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, class_mapper
from sqlalchemy.sql.elements import ColumnElement

_STRINGS_ADAPTER = TypeAdapter(list[str])


def _escape_like(term: str) -> str:
    """Take the wildcards out of what somebody typed.

    Without this a search for `_` or `%` matches every account, which reads as
    the filter being broken rather than as the character meaning something.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def new_signing_key(prefix: str | None = None) -> str:
    return f"{prefix or 'sign_'}{secrets.token_urlsafe(32)}"


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


class WorkspaceAuditRecord(ContractModel):
    id: str
    workspace_id: str
    action: WorkspaceAuditAction
    actor_token_id: str | None = None
    actor_user_id: str | None = None
    """Account behind the change, kept because a token can be revoked and a person cannot."""

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


@dataclass(slots=True)
class UserRepository:
    session: Session

    def create(
        self,
        *,
        display_name: str = "",
        email: str = "",
        avatar_url: str = "",
        role: PlatformRole = PlatformRole.Member,
    ) -> UserRecord:
        row = UserTable(
            display_name=display_name,
            email=email,
            avatar_url=avatar_url,
            role=role.value,
            status=UserStatus.Active.value,
        )
        self.session.add(row)
        self.session.flush()
        return user_record_from_table(row)

    def get(self, user_id: str) -> UserRecord | None:
        row = self.session.get(UserTable, user_id)
        return user_record_from_table(row) if row is not None else None

    def lock_active(self, user_id: str) -> UserRecord:
        """Take a share lock on the account so a concurrent disable cannot slip past.

        The workspace branch of token issue fences its owner the same way: without the
        lock a token can be minted against an account another transaction is in the
        middle of disabling.
        """
        row = self.session.scalars(
            select(UserTable)
            .where(UserTable.id == user_id)
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        ).first()
        if row is None:
            raise NotFoundError(f"user not found: {user_id}")
        record = user_record_from_table(row)
        if record.status is not UserStatus.Active:
            raise NotFoundError(f"user not found: {user_id}")
        return record

    def for_ids(self, user_ids: Collection[str]) -> dict[str, UserRecord]:
        """Resolve many accounts at once, so a listing is not N queries."""
        if not user_ids:
            return {}
        rows = self.session.scalars(select(UserTable).where(UserTable.id.in_(list(user_ids))))
        return {str(row.id): user_record_from_table(row) for row in rows}

    def list(self) -> list[UserRecord]:
        rows = self.session.scalars(
            select(UserTable).order_by(UserTable.created_at.desc(), UserTable.id.asc())
        )
        return [user_record_from_table(row) for row in rows]

    def page(
        self,
        *,
        after_user_id: str | None,
        limit: int,
        search: str = "",
        role: PlatformRole | None = None,
        status: UserStatus | None = None,
    ) -> list[UserRecord]:
        """Accounts, walked by id in one total order, narrowed by what was asked for.

        A keyset walk for the same reason the billing sweeps take one: the
        caller is a list that continues where the last page stopped, and an
        offset would skip or repeat a row for every account created underneath
        it. `None` starts the walk and is an absent predicate, since the column
        is a native UUID and no string stands for "before every id".

        The narrowing is part of the same statement rather than applied to the
        page afterwards. Filtering a page that was already cut to `limit` would
        return fewer rows than asked for and, worse, would end the walk early
        whenever a whole page failed the filter.
        """

        if limit <= 0:
            return []
        statement = select(UserTable)
        if after_user_id is not None:
            statement = statement.where(UserTable.id > after_user_id)
        if role is not None:
            statement = statement.where(UserTable.role == role.value)
        if status is not None:
            statement = statement.where(UserTable.status == status.value)
        term = search.strip()
        if term:
            # The GitHub login is the name an operator knows somebody by, and it
            # lives on the identity rather than the account, so a search that
            # read only `users` would miss the one term most likely to be typed.
            pattern = f"%{_escape_like(term)}%"
            logins = select(UserIdentityTable.user_id).where(
                UserIdentityTable.subject_login.ilike(pattern, escape="\\")
            )
            statement = statement.where(
                or_(
                    UserTable.display_name.ilike(pattern, escape="\\"),
                    UserTable.email.ilike(pattern, escape="\\"),
                    UserTable.id.in_(logins),
                )
            )
        rows = self.session.scalars(statement.order_by(UserTable.id).limit(limit))
        return [user_record_from_table(row) for row in rows]

    def lock_active_administrator_ids(self) -> list[str]:
        """Every account that administers the platform, locked until commit.

        The set a demotion or a disable is judged against. Locked rather than
        counted so two writes that would each remove the other of the last two
        administrators serialize here, and the one that waits re-reads a row
        the first has already changed. PostgreSQL refuses `FOR UPDATE` beside
        an aggregate, so the ids are locked and the caller counts them.
        """

        return list(
            self.session.scalars(
                select(UserTable.id)
                .where(
                    UserTable.role == PlatformRole.Administrator.value,
                    UserTable.status == UserStatus.Active.value,
                )
                .with_for_update()
            ).all()
        )

    def set_profile(
        self,
        user_id: str,
        *,
        display_name: str,
        email: str,
        avatar_url: str,
    ) -> UserRecord:
        return self._update(
            user_id,
            display_name=display_name,
            email=email,
            avatar_url=avatar_url,
        )

    def set_status(self, user_id: str, *, status: UserStatus) -> UserRecord:
        return self._update(user_id, status=status.value)

    def set_role(self, user_id: str, *, role: PlatformRole) -> UserRecord:
        return self._update(user_id, role=role.value)

    def _update(self, user_id: str, **columns: object) -> UserRecord:
        row = self.session.get(UserTable, user_id)
        if row is None:
            raise NotFoundError(f"user not found: {user_id}")
        for column, value in columns.items():
            setattr(row, column, value)
        row.updated_at = utc_now()
        self.session.flush()
        return user_record_from_table(row)


@dataclass(slots=True)
class UserIdentityRepository:
    session: Session

    def lock_subject(self, *, provider: IdentityProvider, subject: str) -> None:
        """Serialize concurrent first sign-ins for one external account.

        Two browsers finishing the flow at once would both read "no account yet"
        and both insert. The unique constraint is what guarantees only one wins;
        this is what keeps the loser from having to unwind a user row it already
        created. SQLite takes a single writer at a time and needs neither.
        """
        if self.session.bind is None or self.session.bind.dialect.name != "postgresql":
            return
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"user-identity:{provider.value}:{subject}"},
        )

    def by_subject(
        self,
        *,
        provider: IdentityProvider,
        subject: str,
    ) -> UserIdentityRecord | None:
        row = self.session.scalars(
            select(UserIdentityTable).where(
                UserIdentityTable.provider == provider.value,
                UserIdentityTable.subject == subject,
            )
        ).first()
        return user_identity_record_from_table(row) if row is not None else None

    def for_user(
        self,
        user_id: str,
        *,
        provider: IdentityProvider = IdentityProvider.Github,
    ) -> UserIdentityRecord | None:
        row = self.session.scalars(
            select(UserIdentityTable).where(
                UserIdentityTable.provider == provider.value,
                UserIdentityTable.user_id == user_id,
            )
        ).first()
        return user_identity_record_from_table(row) if row is not None else None

    def for_users(
        self,
        user_ids: Collection[str],
        *,
        provider: IdentityProvider = IdentityProvider.Github,
    ) -> dict[str, UserIdentityRecord]:
        """Resolve many accounts' identities at once, so a listing is not N queries."""
        if not user_ids:
            return {}
        rows = self.session.scalars(
            select(UserIdentityTable).where(
                UserIdentityTable.provider == provider.value,
                UserIdentityTable.user_id.in_(list(user_ids)),
            )
        )
        records = [user_identity_record_from_table(row) for row in rows]
        return {record.user_id: record for record in records}

    def link(
        self,
        *,
        user_id: str,
        provider: IdentityProvider,
        subject: str,
        subject_login: str = "",
        provider_account_created_at: datetime | None = None,
    ) -> UserIdentityRecord:
        row = UserIdentityTable(
            user_id=user_id,
            provider=provider.value,
            subject=subject,
            subject_login=subject_login,
            provider_account_created_at=provider_account_created_at,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
        except IntegrityError as exc:
            raise ConflictError(
                f"{provider.value} identity {subject} is already linked to an account"
            ) from exc
        self.session.flush()
        return user_identity_record_from_table(row)

    def record_authentication(
        self,
        identity_id: str,
        *,
        subject_login: str,
        authenticated_at: datetime,
    ) -> UserIdentityRecord:
        row = self.session.get(UserIdentityTable, identity_id)
        if row is None:
            raise NotFoundError(f"user identity not found: {identity_id}")
        row.subject_login = subject_login
        row.last_authenticated_at = authenticated_at
        row.updated_at = utc_now()
        self.session.flush()
        return user_identity_record_from_table(row)


@dataclass(slots=True)
class WorkspaceMemberRepository:
    session: Session

    def is_member_for_owner(self, *, owner_user_id: str, member_user_id: str) -> bool:
        owned = WorkspaceMemberTable.__table__.alias("owned_workspace_members")
        members = WorkspaceMemberTable.__table__.alias("account_workspace_members")
        return (
            self.session.scalar(
                select(members.c.id)
                .select_from(
                    owned.join(
                        members,
                        members.c.workspace_id == owned.c.workspace_id,
                    )
                )
                .where(
                    owned.c.user_id == owner_user_id,
                    owned.c.role == WorkspaceRole.Owner.value,
                    members.c.user_id == member_user_id,
                )
                .limit(1)
            )
            is not None
        )

    def distinct_member_count_for_owner(self, owner_user_id: str) -> int:
        """Distinct people reaching any workspace this account owns."""

        owned = WorkspaceMemberTable.__table__.alias("owned_workspace_members")
        members = WorkspaceMemberTable.__table__.alias("account_workspace_members")
        return int(
            self.session.scalar(
                select(func.count(func.distinct(members.c.user_id)))
                .select_from(
                    owned.join(
                        members,
                        members.c.workspace_id == owned.c.workspace_id,
                    )
                )
                .where(
                    owned.c.user_id == owner_user_id,
                    owned.c.role == WorkspaceRole.Owner.value,
                )
            )
            or 0
        )

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

    def owned_workspace_count(self, user_id: str) -> int:
        """How many workspaces this account backs that still exist.

        What a plan's workspace limit is checked against. A workspace being
        deleted is out already: counting it would refuse the workspace somebody
        makes right after deleting one, for a slot that frees itself moments
        later.
        """

        return int(
            self.session.scalar(
                select(func.count(WorkspaceMemberTable.workspace_id))
                .join(WorkspaceTable, WorkspaceTable.id == WorkspaceMemberTable.workspace_id)
                .where(
                    WorkspaceMemberTable.user_id == user_id,
                    WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
                    WorkspaceTable.status.not_in(
                        [WorkspaceStatus.Deleting.value, WorkspaceStatus.Deleted.value]
                    ),
                )
            )
            or 0
        )

    def workspaces_for_user(self, user_id: str) -> list[WorkspaceRecord]:
        """Active workspaces this person reaches, resolved in one query.

        A deleting or deleted workspace is excluded here rather than by the caller:
        every consumer wants the set a person may actually act on.
        """
        rows = self.session.scalars(
            select(WorkspaceTable)
            .join(WorkspaceMemberTable, WorkspaceMemberTable.workspace_id == WorkspaceTable.id)
            .where(
                WorkspaceMemberTable.user_id == user_id,
                WorkspaceTable.status == WorkspaceStatus.Active.value,
            )
            .order_by(WorkspaceTable.created_at.asc())
        )
        return [WorkspaceRecord.model_validate(row.payload) for row in rows]

    def owned_workspace(self, user_id: str) -> WorkspaceRecord | None:
        """The workspace this account owns, which is the one it was given.

        Ownership rather than "their only membership": joining somebody else's
        workspace must not change which one is theirs. Ordered by the membership
        like `owned_workspace_ids` so the two cannot disagree about which comes
        first, and narrowed to active because a caller acts on this one.
        """
        row = self.session.scalars(
            select(WorkspaceTable)
            .join(WorkspaceMemberTable, WorkspaceMemberTable.workspace_id == WorkspaceTable.id)
            .where(
                WorkspaceMemberTable.user_id == user_id,
                WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
                WorkspaceTable.status == WorkspaceStatus.Active.value,
            )
            .order_by(WorkspaceMemberTable.created_at.asc())
        ).first()
        return None if row is None else WorkspaceRecord.model_validate(row.payload)

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

    def member_with_email(self, *, workspace_id: str, email: str) -> WorkspaceMemberRecord | None:
        """A member whose provider-reported address is this one, if any.

        Email is not unique across accounts, so this answers "is somebody with this
        address already in" for an invitation, and nothing about who that person is.
        """
        row = self.session.scalars(
            select(WorkspaceMemberTable)
            .join(UserTable, UserTable.id == WorkspaceMemberTable.user_id)
            .where(
                WorkspaceMemberTable.workspace_id == workspace_id,
                func.lower(UserTable.email) == email,
            )
        ).first()
        return workspace_member_record_from_table(row) if row is not None else None

    def member_user_id_for_owner_email(self, *, owner_user_id: str, email: str) -> str | None:
        """An account with this address already seated somewhere this owner pays for.

        Inviting that address to a second workspace of the same owner takes no new
        seat, which is the same allowance adding them by id gets.
        """
        owned = WorkspaceMemberTable.__table__.alias("owned_workspace_members")
        members = WorkspaceMemberTable.__table__.alias("account_workspace_members")
        value = self.session.scalar(
            select(members.c.user_id)
            .select_from(
                owned.join(members, members.c.workspace_id == owned.c.workspace_id).join(
                    UserTable, UserTable.id == members.c.user_id
                )
            )
            .where(
                owned.c.user_id == owner_user_id,
                owned.c.role == WorkspaceRole.Owner.value,
                func.lower(UserTable.email) == email,
            )
            .limit(1)
        )
        return str(value) if value is not None else None


@dataclass(slots=True)
class WorkspaceInvitationRepository:
    session: Session

    def create(
        self,
        *,
        workspace_id: str,
        email: str,
        role: WorkspaceInvitationRole,
        invited_by_user_id: str,
        token_hash: str,
        expires_at: datetime,
        message_id: str,
    ) -> WorkspaceInvitationRecord:
        """Write one open offer. The unique constraint is what refuses a second."""
        row = WorkspaceInvitationTable(
            workspace_id=workspace_id,
            email=email,
            role=role.value,
            token_hash=token_hash,
            invited_by_user_id=invited_by_user_id or None,
            expires_at=expires_at,
            message_id=message_id or None,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                f"an invitation for {email} is already open. Resend that one instead"
            ) from exc
        return workspace_invitation_record_from_table(row)

    def lock(self, invitation_id: str) -> WorkspaceInvitationRecord | None:
        """The row, held against a concurrent answer to the same offer."""
        return self._locked(WorkspaceInvitationTable.id == invitation_id)

    def lock_by_token(self, token_hash: str) -> WorkspaceInvitationRecord | None:
        """The offer a link presents, held so two clicks cannot both redeem it."""
        return self._locked(WorkspaceInvitationTable.token_hash == token_hash)

    def by_token(self, token_hash: str) -> WorkspaceInvitationRecord | None:
        """The same lookup without the lock, for showing an offer before answering it."""
        row = self.session.scalars(
            select(WorkspaceInvitationTable).where(
                WorkspaceInvitationTable.token_hash == token_hash
            )
        ).first()
        return workspace_invitation_record_from_table(row) if row is not None else None

    def for_workspace(self, workspace_id: str) -> list[WorkspaceInvitationRecord]:
        """Every open offer, expired ones included: those are still an admin's to act on."""
        rows = self.session.scalars(
            select(WorkspaceInvitationTable)
            .where(WorkspaceInvitationTable.workspace_id == workspace_id)
            .order_by(WorkspaceInvitationTable.created_at.asc())
        )
        return [workspace_invitation_record_from_table(row) for row in rows]

    def reissue(
        self,
        invitation_id: str,
        *,
        token_hash: str,
        expires_at: datetime,
        message_id: str,
    ) -> WorkspaceInvitationRecord:
        """Give the offer a new secret and a fresh expiry.

        The old link stops working, which is the point: a resend exists because
        the first message went astray, and leaving its link live would keep
        whatever went astray with it usable.
        """
        row = self._row(invitation_id)
        row.token_hash = token_hash
        row.expires_at = expires_at
        row.message_id = message_id or None
        row.updated_at = utc_now()
        self.session.flush()
        return workspace_invitation_record_from_table(row)

    def delete(self, invitation_id: str) -> bool:
        """Take the offer off the table, however it was answered.

        Nothing keeps answered offers: a membership records an acceptance and the
        workspace audit history records every outcome, so a retained row would be
        a second account of the same event with nothing keeping the two in step.
        """
        result = self.session.execute(
            delete(WorkspaceInvitationTable).where(WorkspaceInvitationTable.id == invitation_id)
        )
        self.session.flush()
        return isinstance(result, CursorResult) and result.rowcount > 0

    def open_email_count_for_owner(self, owner_user_id: str, *, now: datetime) -> int:
        """Addresses holding a live offer into any workspace this account owns.

        Excludes addresses already seated in one of those workspaces, so an offer
        to somebody who is counted as a member is not counted twice.
        """
        owned = WorkspaceMemberTable.__table__.alias("owned_workspace_members")
        seated = WorkspaceMemberTable.__table__.alias("seated_workspace_members")
        seated_emails = (
            select(func.lower(UserTable.email))
            .select_from(
                owned.join(seated, seated.c.workspace_id == owned.c.workspace_id).join(
                    UserTable, UserTable.id == seated.c.user_id
                )
            )
            .where(owned.c.user_id == owner_user_id, owned.c.role == WorkspaceRole.Owner.value)
        )
        return int(
            self.session.scalar(
                select(func.count(func.distinct(WorkspaceInvitationTable.email)))
                .select_from(
                    owned.join(
                        WorkspaceInvitationTable,
                        WorkspaceInvitationTable.workspace_id == owned.c.workspace_id,
                    )
                )
                .where(
                    owned.c.user_id == owner_user_id,
                    owned.c.role == WorkspaceRole.Owner.value,
                    WorkspaceInvitationTable.expires_at > now,
                    WorkspaceInvitationTable.email.not_in(seated_emails),
                )
            )
            or 0
        )

    def _locked(self, condition: ColumnElement[bool]) -> WorkspaceInvitationRecord | None:
        row = self.session.scalars(
            select(WorkspaceInvitationTable)
            .where(condition)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        return workspace_invitation_record_from_table(row) if row is not None else None

    def _row(self, invitation_id: str) -> WorkspaceInvitationTable:
        row = self.session.get(WorkspaceInvitationTable, invitation_id)
        if row is None:
            raise NotFoundError(f"invitation not found: {invitation_id}")
        return row


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
        """The workspace that currently holds this name, in whatever state.

        A deleted tombstone holds nothing: it kept its row so the ledger, usage and
        audit history pointing at it stay readable, and released the name so the
        next workspace can take it. The partial unique index is what makes "the"
        singular here.
        """
        row = self.session.scalars(
            select(WorkspaceTable).where(
                WorkspaceTable.name == name,
                WorkspaceTable.status != WorkspaceStatus.Deleted.value,
            )
        ).first()
        return WorkspaceRecord.model_validate(row.payload) if row is not None else None

    def resolve_for_deletion(self, workspace_id_or_name: str) -> WorkspaceRecord | None:
        """System lookup by id that retains tombstones, or by whoever holds the name."""
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
            .where(
                WorkspaceTable.id == workspace_id,
                WorkspaceTable.status == WorkspaceStatus.Active.value,
            )
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        ).first()
        return _workspace_record(row, workspace_id)

    def lock_object_write_completion_owner(self, workspace_id: str) -> WorkspaceRecord:
        """Fence completion of a write admitted before deletion began."""
        row = self.session.scalars(
            select(WorkspaceTable)
            .where(
                WorkspaceTable.id == workspace_id,
                WorkspaceTable.status.in_(
                    (
                        WorkspaceStatus.Active.value,
                        WorkspaceStatus.Disabled.value,
                        WorkspaceStatus.Deleting.value,
                    )
                ),
            )
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        ).first()
        return _workspace_record(row, workspace_id)

    def lock_storage_accounting_owner(self, workspace_id: str) -> WorkspaceRecord:
        """Stored bytes accrue usage until removal, even while a workspace is disabled."""
        row = self.session.scalar(
            select(WorkspaceTable)
            .where(
                WorkspaceTable.id == workspace_id,
                WorkspaceTable.status.in_(
                    (
                        WorkspaceStatus.Active.value,
                        WorkspaceStatus.Disabled.value,
                        WorkspaceStatus.Deleting.value,
                    )
                ),
            )
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        )
        return _workspace_record(row, workspace_id)

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
                raise ConflictError(f"workspace is not active: {name}")
            return current
        return self.create(name=name, signing_key=signing_key)


def _workspace_purge_excluded_tables() -> set[str]:
    """Tables a workspace owns rows in that its deletion does not clear.

    Two reasons, and both are why `deletion_blockers` reads the same set: identity
    and history the workspace lifecycle owns elsewhere, and the money. The priced
    ledger, the unsent meter events and the placements they price against are
    what a customer owed, so a deletion that removed them would destroy the
    evidence for a charge while carefully keeping the usage it derives from.
    """
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
        _mapped_table_name(ContainerBillingShapeTable),
        _mapped_table_name(BillingLedgerSegmentTable),
        _mapped_table_name(BillingMeterOutboxTable),
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
                "actor_user_id": actor.user_id or None,
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
            actor_user_id=actor.user_id or None,
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
                actor_user_id=actor.user_id or None,
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


@dataclass(frozen=True, slots=True)
class AccountTokenCursor:
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class AccountTokenPage:
    records: tuple[AuthTokenRecord, ...]
    next: AccountTokenCursor | None = None


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

    def get_for_user(self, token_id: str, *, user_id: str) -> AuthTokenRecord | None:
        row = self.session.scalars(
            select(TokenTable).where(
                TokenTable.id == token_id,
                TokenTable.user_id == user_id,
            )
        ).first()
        return auth_token_record_from_table(row) if row is not None else None

    def list_manageable_for_user(
        self,
        user_id: str,
        *,
        limit: int,
        cursor: AccountTokenCursor | None = None,
    ) -> AccountTokenPage:
        """The credentials a person deliberately created, which are theirs to manage.

        Their sessions are not among them: a session is what being signed in *is*, it
        is ended by signing out, and listing one beside a credential invites revoking
        the browser you are reading the list in. The offline administrator credential
        is not either, because it is minted and rotated outside the product.

        A revoked credential is kept but not listed. Its row is the only record that
        it ever existed, when it was made, and when it stopped working, so removing
        the row would destroy the account's own history of it.
        """
        statement = select(TokenTable).where(
            TokenTable.user_id == user_id,
            TokenTable.kind == TokenKind.User.value,
            TokenTable.status == TokenStatus.Active.value,
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    TokenTable.created_at < cursor.created_at,
                    and_(
                        TokenTable.created_at == cursor.created_at,
                        TokenTable.id > cursor.id,
                    ),
                )
            )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    TokenTable.created_at.desc(),
                    TokenTable.id.asc(),
                ).limit(limit + 1)
            )
        )
        page_rows = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page_rows:
            last = page_rows[-1]
            next_cursor = AccountTokenCursor(created_at=last.created_at, id=str(last.id))
        return AccountTokenPage(
            records=tuple(auth_token_record_from_table(row) for row in page_rows),
            next=next_cursor,
        )

    def revoke_for_user(
        self,
        token_id: str,
        *,
        user_id: str,
        now: datetime,
    ) -> AuthTokenRecord | None:
        row = self.session.scalars(
            update(TokenTable)
            .where(
                TokenTable.id == token_id,
                TokenTable.user_id == user_id,
                TokenTable.kind == TokenKind.User.value,
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

    def revoke_user_credentials(self, user_id: str, *, now: datetime) -> int:
        """End every live credential naming a person, which is what disabling means.

        Wider than ending their sessions: a device login mints a long-lived account
        credential, so revoking only the browser's would leave the CLI authenticating
        as an account that is no longer allowed to act.
        """
        result = self.session.execute(
            update(TokenTable)
            .where(
                TokenTable.user_id == user_id,
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
        """Drop a platform service credential so its replacement can take the name."""
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
