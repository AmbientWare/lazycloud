from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.identity import TokenRepository, WorkspaceMemberRepository
from database.tables.identity import TokenTable
from identity.users import UserService
from shared.errors import ConflictError
from shared.identity import TokenKind, WorkspaceRole
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError


def test_a_token_names_exactly_one_principal(isolated_services: ApiServices) -> None:
    """A credential reaches an account's workspaces or one workspace, never both.

    The two answer different questions about what may be touched, so a row carrying
    both would have two. Held by the schema because a rule enforced only in Python is
    enforced only where somebody remembered to call it.
    """
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("principal")
    user = UserService(isolated_services.context).create(
        username="principal-owner",
        password="principal-owner-password",
    )
    with isolated_services.context.database.session() as session:
        repository = TokenRepository(session)
        by_user = repository.create(
            name="user-token",
            token_hash="hash-user",
            prefix="rt_user",
            kind=TokenKind.User,
            user_id=user.id,
        )
        by_workspace = repository.create(
            name="workspace-token",
            token_hash="hash-workspace",
            prefix="rt_ws",
            kind=TokenKind.Workspace,
            workspace_id=workspace.id,
        )
    assert by_user.user_id == user.id and by_user.workspace_id == ""
    assert by_workspace.workspace_id == workspace.id and by_workspace.user_id == ""

    with (
        pytest.raises(IntegrityError),
        isolated_services.context.database.session() as session,
    ):
        session.execute(
            update(TokenTable).where(TokenTable.id == by_user.id).values(workspace_id=workspace.id)
        )


def test_a_workspace_has_at_most_one_owner(isolated_services: ApiServices) -> None:
    """The owner is who the connected compute and the domains resolve through.

    A second one would make "whose account backs this workspace" have two answers, so
    the schema refuses it rather than leaving the choice to whichever query ran first.
    """
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("sole-owner")
    users = UserService(isolated_services.context)
    first = users.create(username="first-owner", password="first-owner-password")
    second = users.create(username="second-owner", password="second-owner-password")

    with isolated_services.context.database.session() as session:
        repository = WorkspaceMemberRepository(session)
        repository.add(
            workspace_id=workspace.id,
            user_id=first.id,
            role=WorkspaceRole.Owner,
        )

    with (
        pytest.raises(ConflictError, match="already has an owner"),
        isolated_services.context.database.session() as session,
    ):
        WorkspaceMemberRepository(session).add(
            workspace_id=workspace.id,
            user_id=second.id,
            role=WorkspaceRole.Owner,
        )

    with isolated_services.context.database.session() as session:
        repository = WorkspaceMemberRepository(session)
        repository.add(
            workspace_id=workspace.id,
            user_id=second.id,
            role=WorkspaceRole.Member,
        )
        owner = repository.owner(workspace.id)
    assert owner is not None and owner.user_id == first.id
