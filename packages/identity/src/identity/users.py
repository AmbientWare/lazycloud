from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.identity import (
    TokenRepository,
    UserRepository,
    WorkspaceMemberRepository,
)
from database.types import DatabaseSession
from shared.errors import ConflictError, NotFoundError
from shared.identity import (
    AuthTokenRecord,
    PlatformRole,
    TokenKind,
    UserRecord,
    UserStatus,
    WorkspaceMemberRecord,
    WorkspaceRecord,
    WorkspaceRole,
)
from shared.timestamps import utc_now
from sqlalchemy.exc import IntegrityError

from identity.auth import AuthError, IdentityContext, TokenIssuer
from identity.passwords import (
    ABSENT_USER_HASH,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)

SESSION_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    token: str
    record: AuthTokenRecord
    user: UserRecord
    expires_at: datetime


class UserService:
    """Users, their credentials, and which workspaces each one reaches."""

    def __init__(self, context: IdentityContext) -> None:
        self.context = context

    def create(
        self,
        *,
        username: str,
        password: str,
        role: PlatformRole = PlatformRole.Member,
    ) -> UserRecord:
        normalized = validate_username(username)
        password_hash = hash_password(validate_password(password))
        with self.context.database.session() as session:
            try:
                return UserRepository(session).create(
                    username=normalized,
                    password_hash=password_hash,
                    role=role,
                )
            except IntegrityError as exc:
                raise ConflictError(f"username is already in use: {normalized}") from exc

    def get(self, user_id: str) -> UserRecord:
        with self.context.database.session() as session:
            user = UserRepository(session).get(user_id)
        if user is None:
            raise NotFoundError(f"user not found: {user_id}")
        return user

    def by_username(self, username: str) -> UserRecord:
        with self.context.database.session() as session:
            user = UserRepository(session).by_username(username)
        if user is None:
            raise NotFoundError(f"user not found: {username}")
        return user

    def list(self) -> list[UserRecord]:
        with self.context.database.session() as session:
            return UserRepository(session).list()

    def change_password(self, user_id: str, *, password: str) -> UserRecord:
        """Set a new password and end every session minted under the old one."""
        password_hash = hash_password(validate_password(password))
        now = utc_now()
        with self.context.database.session() as session:
            repository = UserRepository(session)
            if repository.get(user_id) is None:
                raise NotFoundError(f"user not found: {user_id}")
            updated = repository.set_password(user_id, password_hash=password_hash)
            TokenRepository(session).revoke_user_sessions(user_id, now=now)
        return updated

    def set_status(self, user_id: str, *, status: UserStatus) -> UserRecord:
        now = utc_now()
        with self.context.database.session() as session:
            repository = UserRepository(session)
            if repository.get(user_id) is None:
                raise NotFoundError(f"user not found: {user_id}")
            updated = repository.set_status(user_id, status=status)
            if status is UserStatus.Disabled:
                TokenRepository(session).revoke_user_sessions(user_id, now=now)
        return updated

    def workspaces(self, user_id: str) -> list[WorkspaceRecord]:
        with self.context.database.session() as session:
            return WorkspaceMemberRepository(session).workspaces_for_user(user_id)

    def membership(self, *, workspace_id: str, user_id: str) -> WorkspaceMemberRecord | None:
        with self.context.database.session() as session:
            return WorkspaceMemberRepository(session).membership(
                workspace_id=workspace_id,
                user_id=user_id,
            )

    def add_member(
        self,
        *,
        workspace_id: str,
        user_id: str,
        role: WorkspaceRole = WorkspaceRole.Member,
    ) -> WorkspaceMemberRecord:
        with self.context.database.session() as session:
            if UserRepository(session).get(user_id) is None:
                raise NotFoundError(f"user not found: {user_id}")
            return WorkspaceMemberRepository(session).add(
                workspace_id=workspace_id,
                user_id=user_id,
                role=role,
            )

    def members(self, workspace_id: str) -> list[WorkspaceMemberRecord]:
        with self.context.database.session() as session:
            return WorkspaceMemberRepository(session).for_workspace(workspace_id)

    def set_member_role(
        self,
        *,
        workspace_id: str,
        user_id: str,
        role: WorkspaceRole,
    ) -> WorkspaceMemberRecord:
        with self.context.database.session() as session:
            repository = WorkspaceMemberRepository(session)
            membership = repository.membership(workspace_id=workspace_id, user_id=user_id)
            if membership is None:
                raise NotFoundError(f"user is not a member of this workspace: {user_id}")
            if membership.role is WorkspaceRole.Owner:
                raise ConflictError(
                    "the workspace owner's role cannot be changed; transfer ownership first"
                )
            repository.set_role(workspace_id=workspace_id, user_id=user_id, role=role)
            updated = repository.membership(workspace_id=workspace_id, user_id=user_id)
        if updated is None:
            raise NotFoundError(f"user is not a member of this workspace: {user_id}")
        return updated

    def remove_member(self, *, workspace_id: str, user_id: str) -> bool:
        with self.context.database.session() as session:
            repository = WorkspaceMemberRepository(session)
            membership = repository.membership(workspace_id=workspace_id, user_id=user_id)
            if membership is None:
                return False
            if membership.role is WorkspaceRole.Owner:
                # The owner is who the connected compute account and the domains resolve
                # through, so removing them would leave the workspace with no account.
                raise ConflictError(
                    "the workspace owner cannot be removed; transfer ownership first"
                )
            return repository.remove(workspace_id=workspace_id, user_id=user_id)

    def owner_user_id(self, workspace_id: str) -> str:
        """The account backing a workspace: whose compute and domains it resolves to."""
        with self.context.database.session() as session:
            return WorkspaceMemberRepository(session).owner_user_id(workspace_id)

    def owned_workspace_ids(self, user_id: str) -> list[str]:
        """The workspaces an account's compute and domains apply to, in creation order."""
        with self.context.database.session() as session:
            return WorkspaceMemberRepository(session).owned_workspace_ids(user_id)


class SessionService:
    """Password sign-in, and the short-lived credential it hands back."""

    def __init__(
        self,
        context: IdentityContext,
        *,
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        self.context = context
        self.ttl_seconds = ttl_seconds

    def sign_in(self, *, username: str, password: str) -> AuthenticatedSession:
        issuer = TokenIssuer(self.context)
        issued = False
        with self.context.database.session() as session:
            user = self._verified_user(session, username=username, password=password)
            raw_token, record = issuer.issue_for_user(
                session,
                f"session:{user.username}",
                kind=TokenKind.Session,
                user_id=user.id,
                expires_in_seconds=self.ttl_seconds,
                reusable=True,
            )
            issued = True
        if issued:
            issuer.committed()
        expires_at = record.expires_at or (utc_now() + timedelta(seconds=self.ttl_seconds))
        return AuthenticatedSession(
            token=raw_token,
            record=record,
            user=user,
            expires_at=expires_at,
        )

    def verify_password(self, *, user_id: str, password: str) -> UserRecord:
        """Prove the caller holds an account's current password.

        Used before a self-service password change, so a borrowed session cannot be
        turned into permanent ownership of the account.
        """
        with self.context.database.session() as session:
            user = UserRepository(session).get(user_id)
        if user is None:
            raise AuthError("invalid username or password")
        if not verify_password(password, user.password_hash):
            raise AuthError("invalid username or password")
        return user

    def _verified_user(
        self,
        session: DatabaseSession,
        *,
        username: str,
        password: str,
    ) -> UserRecord:
        user = UserRepository(session).by_username(username)
        # Both branches run one verify, so a username that does not exist costs the
        # same as one whose password is wrong.
        if user is None:
            verify_password(password, ABSENT_USER_HASH)
            raise AuthError("invalid username or password")
        if not verify_password(password, user.password_hash):
            raise AuthError("invalid username or password")
        if user.status is not UserStatus.Active:
            raise AuthError("this account is disabled")
        return user


__all__ = [
    "SESSION_TTL_SECONDS",
    "AuthenticatedSession",
    "SessionService",
    "UserService",
]
