from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from identity.auth import AuthError, AuthService
from identity.authz import decide_authorization, workspace_requirement
from identity.users import SessionService, UserService
from shared.identity import (
    PlatformRole,
    TokenKind,
    UserStatus,
    WorkspaceMemberRecord,
    WorkspaceRole,
)

PASSWORD = "correct-horse-battery"


def _users(services: ApiServices) -> UserService:
    return UserService(services.context)


def test_sign_in_refuses_every_credential_that_is_not_the_right_one(
    isolated_services: ApiServices,
) -> None:
    """Wrong password, unknown username, and a disabled account all fail the same way.

    They are three different reasons and one message, because telling them apart is
    how an attacker learns which usernames exist.
    """
    users = _users(isolated_services)
    sessions = SessionService(isolated_services.context)
    users.create(username="operator", password=PASSWORD)

    with pytest.raises(AuthError, match="invalid username or password"):
        sessions.sign_in(username="operator", password="not-the-password")
    with pytest.raises(AuthError, match="invalid username or password"):
        sessions.sign_in(username="nobody", password=PASSWORD)

    signed_in = sessions.sign_in(username="operator", password=PASSWORD)
    assert signed_in.record.kind is TokenKind.Session
    assert signed_in.record.user_id == signed_in.user.id
    assert signed_in.record.workspace_id == ""
    assert signed_in.expires_at > signed_in.record.created_at

    users.set_status(signed_in.user.id, status=UserStatus.Disabled)
    with pytest.raises(AuthError, match="disabled"):
        sessions.sign_in(username="operator", password=PASSWORD)


def test_changing_a_password_ends_the_sessions_minted_under_the_old_one(
    isolated_services: ApiServices,
) -> None:
    """Otherwise a stolen session outlives the password change made to revoke it."""
    users = _users(isolated_services)
    sessions = SessionService(isolated_services.context)
    user = users.create(username="operator", password=PASSWORD)
    signed_in = sessions.sign_in(username="operator", password=PASSWORD)
    auth = AuthService(isolated_services.context)
    assert auth.authenticate(signed_in.token).id == signed_in.record.id

    users.change_password(user.id, password="a-different-long-password")
    auth.credentials_revoked()

    with pytest.raises(AuthError, match="invalid token"):
        auth.authenticate(signed_in.token)
    assert sessions.sign_in(username="operator", password="a-different-long-password").token


def test_a_users_credential_reaches_only_the_workspaces_they_belong_to(
    isolated_services: ApiServices,
) -> None:
    """Membership is the whole of a person's reach, and the role bounds what they may do.

    This is the isolation boundary the account model rests on: one customer's
    credential must not act in another customer's workspace, and a member must not
    perform an action reserved for the owner.
    """
    control = ControlPlaneService(isolated_services.context)
    mine = control.upsert_workspace("mine")
    theirs = control.upsert_workspace("theirs")
    users = _users(isolated_services)
    me = users.create(username="me-user", password=PASSWORD)
    them = users.create(username="them-user", password=PASSWORD)
    users.add_member(workspace_id=mine.id, user_id=me.id, role=WorkspaceRole.Owner)
    users.add_member(workspace_id=theirs.id, user_id=them.id, role=WorkspaceRole.Owner)

    my_token = (
        SessionService(isolated_services.context)
        .sign_in(
            username="me-user",
            password=PASSWORD,
        )
        .record
    )

    assert decide_authorization(
        my_token,
        workspace_requirement(
            mine.id,
            membership=users.membership(
                workspace_id=mine.id,
                user_id=me.id,
            ),
        ),
    ).allowed
    denied = decide_authorization(
        my_token,
        workspace_requirement(
            theirs.id,
            membership=users.membership(
                workspace_id=theirs.id,
                user_id=me.id,
            ),
        ),
    )
    assert not denied.allowed

    # A membership row naming somebody else cannot stand in for one's own.
    borrowed = decide_authorization(
        my_token,
        workspace_requirement(
            theirs.id,
            membership=WorkspaceMemberRecord(
                id="borrowed",
                workspace_id=theirs.id,
                user_id=them.id,
                role=WorkspaceRole.Owner,
            ),
        ),
    )
    assert not borrowed.allowed

    users.add_member(workspace_id=theirs.id, user_id=me.id, role=WorkspaceRole.Member)
    membership = users.membership(workspace_id=theirs.id, user_id=me.id)
    assert decide_authorization(
        my_token,
        workspace_requirement(theirs.id, membership=membership),
    ).allowed
    assert not decide_authorization(
        my_token,
        workspace_requirement(
            theirs.id,
            membership=membership,
            required_role=WorkspaceRole.Owner,
        ),
    ).allowed


def test_a_workspace_credential_cannot_be_widened_by_a_membership_row(
    isolated_services: ApiServices,
) -> None:
    """Automation keeps its single-workspace blast radius whatever else is presented."""
    control = ControlPlaneService(isolated_services.context)
    mine = control.upsert_workspace("mine")
    theirs = control.upsert_workspace("theirs")
    users = _users(isolated_services)
    me = users.create(username="me-user", password=PASSWORD)
    _raw, workspace_token = AuthService(isolated_services.context).create_token(
        "ci",
        workspace_id=mine.id,
    )

    assert not decide_authorization(
        workspace_token,
        workspace_requirement(
            theirs.id,
            membership=WorkspaceMemberRecord(
                id="unrelated",
                workspace_id=theirs.id,
                user_id=me.id,
                role=WorkspaceRole.Owner,
            ),
        ),
        platform_role=PlatformRole.Member,
    ).allowed
