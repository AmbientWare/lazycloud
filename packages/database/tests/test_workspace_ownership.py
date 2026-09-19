from __future__ import annotations

import pytest
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.identity import WorkspaceMemberRepository
from identity.users import UserService
from shared.errors import ConflictError
from shared.identity import WorkspaceRole


def test_a_workspace_has_at_most_one_owner(service_context: ServiceContext) -> None:
    """The owner is who the connected compute and the domains resolve through.

    A second one would make "whose account backs this workspace" have two answers.
    Adding members stays open, so the refusal has to be specific to the owner role
    rather than to membership.
    """
    users = UserService(service_context)
    first = users.create(display_name="first-owner")
    second = users.create(display_name="second-owner")
    workspace = ControlPlaneService(service_context).set_workspace(
        "sole-owner",
        owner_user_id=first.id,
    )

    with (
        pytest.raises(ConflictError, match="already has an owner"),
        service_context.database.session() as session,
    ):
        WorkspaceMemberRepository(session).add(
            workspace_id=workspace.id,
            user_id=second.id,
            role=WorkspaceRole.Owner,
        )

    with service_context.database.session() as session:
        repository = WorkspaceMemberRepository(session)
        repository.add(
            workspace_id=workspace.id,
            user_id=second.id,
            role=WorkspaceRole.Member,
        )
        owner = repository.owner(workspace.id)
    assert owner is not None and owner.user_id == first.id
