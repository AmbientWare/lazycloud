from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.identity import WorkspaceMemberRepository
from identity.users import UserService
from shared.errors import ConflictError
from shared.identity import WorkspaceRole


def test_a_workspace_has_at_most_one_owner(isolated_services: ApiServices) -> None:
    """The owner is who the connected compute and the domains resolve through.

    A second one would make "whose account backs this workspace" have two answers.
    Adding members stays open, so the refusal has to be specific to the owner role
    rather than to membership.
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
