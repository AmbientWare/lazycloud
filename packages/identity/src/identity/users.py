from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from database.repositories.identity import (
    TokenRepository,
    UserIdentityRepository,
    UserRepository,
    WorkspaceAuditRepository,
    WorkspaceMemberRepository,
)
from shared.errors import ConflictError, NotFoundError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import (
    AuthTokenRecord,
    IdentityProvider,
    PlatformRole,
    UserIdentityRecord,
    UserRecord,
    UserStatus,
    WorkspaceMemberRecord,
    WorkspaceRecord,
    WorkspaceRole,
)
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from identity.auth import IdentityContext


class WorkspaceMembershipAdmission(Protocol):
    def assert_may_add_workspace_member(
        self,
        session: Session,
        *,
        workspace_id: str,
        member_user_id: str | None,
    ) -> None: ...


class UserService:
    """Users, the external identities that reach them, and the workspaces they join."""

    def __init__(self, context: IdentityContext) -> None:
        self.context = context

    def create(
        self,
        *,
        display_name: str = "",
        github_user_id: int | None = None,
        github_login: str = "",
        role: PlatformRole = PlatformRole.Member,
    ) -> UserRecord:
        """Create an account, linked to a GitHub identity when one is named.

        Linking here is what makes an administrator-created account reachable: without
        it that person's first sign-in finds no identity and opens a second account
        for them instead, with none of the standing this one was given.
        """
        with self.context.database.session() as session:
            identities = UserIdentityRepository(session)
            subject = str(github_user_id) if github_user_id is not None else ""
            if subject:
                identities.lock_subject(provider=IdentityProvider.Github, subject=subject)
                existing = identities.by_subject(
                    provider=IdentityProvider.Github,
                    subject=subject,
                )
                if existing is not None:
                    raise ConflictError(
                        f"github identity {subject} is already linked to an account"
                    )
            user = UserRepository(session).create(display_name=display_name, role=role)
            if subject:
                identities.link(
                    user_id=user.id,
                    provider=IdentityProvider.Github,
                    subject=subject,
                    subject_login=github_login,
                )
            return user

    def get(self, user_id: str) -> UserRecord:
        with self.context.database.session() as session:
            user = UserRepository(session).get(user_id)
        if user is None:
            raise NotFoundError(f"user not found: {user_id}")
        return user

    def list(self) -> list[UserRecord]:
        with self.context.database.session() as session:
            return UserRepository(session).list()

    def identity(self, user_id: str) -> UserIdentityRecord | None:
        with self.context.database.session() as session:
            return UserIdentityRepository(session).for_user(user_id)

    def identities(self, user_ids: Sequence[str]) -> dict[str, UserIdentityRecord]:
        with self.context.database.session() as session:
            return UserIdentityRepository(session).for_users(user_ids)

    def set_role(self, user_id: str, *, role: PlatformRole) -> UserRecord:
        """Grant or withdraw platform administrator standing on an existing account.

        Signing in creates an ordinary member, so without this the only
        administrators are the ones named at creation and anyone who arrived
        through sign-in is a member permanently.
        """
        with self.context.database.session() as session:
            repository = UserRepository(session)
            if repository.get(user_id) is None:
                raise NotFoundError(f"user not found: {user_id}")
            return repository.set_role(user_id, role=role)

    def set_status(self, user_id: str, *, status: UserStatus) -> UserRecord:
        now = utc_now()
        with self.context.database.session() as session:
            repository = UserRepository(session)
            if repository.get(user_id) is None:
                raise NotFoundError(f"user not found: {user_id}")
            updated = repository.set_status(user_id, status=status)
            if status is UserStatus.Disabled:
                TokenRepository(session).revoke_user_credentials(user_id, now=now)
        return updated

    def workspaces(self, user_id: str) -> list[WorkspaceRecord]:
        with self.context.database.session() as session:
            return WorkspaceMemberRepository(session).workspaces_for_user(user_id)

    def owned_workspace(self, user_id: str) -> WorkspaceRecord | None:
        with self.context.database.session() as session:
            return WorkspaceMemberRepository(session).owned_workspace(user_id)

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
        admission: WorkspaceMembershipAdmission,
    ) -> WorkspaceMemberRecord:
        with self.context.database.session() as session:
            if UserRepository(session).get(user_id) is None:
                raise NotFoundError(f"user not found: {user_id}")
            admission.assert_may_add_workspace_member(
                session,
                workspace_id=workspace_id,
                member_user_id=user_id,
            )
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
        actor: AuthTokenRecord,
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
            if membership.role is role:
                return membership
            repository.set_role(workspace_id=workspace_id, user_id=user_id, role=role)
            updated = repository.membership(workspace_id=workspace_id, user_id=user_id)
            if updated is None:
                raise NotFoundError(f"user is not a member of this workspace: {user_id}")
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace_id,
                action=WorkspaceAuditAction.MemberRoleChanged,
                actor=actor,
                target_type=WorkspaceAuditTarget.Member,
                target_id=user_id,
                target_name=_member_name(session, user_id),
                summary=f"Changed a member's role from {membership.role} to {role}",
                previous_value=membership.role.value,
                new_value=role.value,
            )
        return updated

    def remove_member(
        self,
        *,
        workspace_id: str,
        user_id: str,
        actor: AuthTokenRecord,
    ) -> bool:
        """Take a person out of a workspace, whether an administrator did it or they left.

        Leaving is the same removal by the person themself, and is recorded as
        such: who ended a membership is the fact the audit history is asked for.
        """
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
            removed = repository.remove(workspace_id=workspace_id, user_id=user_id)
            if removed:
                left = actor.user_id == user_id
                WorkspaceAuditRepository(session).append(
                    workspace_id=workspace_id,
                    action=(
                        WorkspaceAuditAction.MemberLeft
                        if left
                        else WorkspaceAuditAction.MemberRemoved
                    ),
                    actor=actor,
                    target_type=WorkspaceAuditTarget.Member,
                    target_id=user_id,
                    target_name=_member_name(session, user_id),
                    summary="Left the workspace" if left else "Removed a member",
                    previous_value=membership.role.value,
                )
            return removed

    def owned_workspace_ids(self, user_id: str) -> list[str]:
        """The workspaces an account's compute and domains apply to, in creation order."""
        with self.context.database.session() as session:
            return WorkspaceMemberRepository(session).owned_workspace_ids(user_id)


def _member_name(session: Session, user_id: str) -> str:
    user = UserRepository(session).get(user_id)
    if user is None:
        return user_id
    return user.display_name or user.email or user_id


__all__ = ["UserService", "WorkspaceMembershipAdmission"]
