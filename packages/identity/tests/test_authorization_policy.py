from __future__ import annotations

import pytest
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.identity import TokenRepository, WorkspaceMemberRepository
from identity.auth import AuthError, AuthService, AuthTokenCache
from identity.authz import (
    AuthzDecisionReason,
    AuthzResourceKind,
    admin_requirement,
    build_policy_input,
    decide_authorization,
    worker_requirement,
    workspace_requirement,
)
from identity.users import UserService
from shared.identity import (
    AuthScope,
    PlatformRole,
    TokenKind,
    WorkspaceMemberRecord,
    WorkspaceRole,
)
from tests.workspaces import administrator_credential, owned_workspace


def test_auth_service_records_token_kind_and_checks_scopes(
    service_context: ServiceContext,
) -> None:
    workspace = owned_workspace(ControlPlaneService(service_context), "workspace-a")
    cache = AuthTokenCache()
    auth = AuthService(service_context, token_cache=cache)
    raw_token, record = auth.create_token(
        "restricted",
        scopes=[AuthScope.Read.value],
        kind=TokenKind.WorkspaceRestricted,
        workspace_id=workspace.id,
        reusable=False,
    )

    assert record.kind == TokenKind.WorkspaceRestricted
    assert record.workspace_id == workspace.id
    assert not record.reusable
    with pytest.raises(AuthError, match="missing scope"):
        auth.authenticate(raw_token, scope=AuthScope.Write)
    assert auth.authenticate(raw_token, scope=AuthScope.Read).id == record.id

    with pytest.raises(AuthError, match="invalid token"):
        auth.authenticate(raw_token, scope=AuthScope.Read)


def test_bootstrap_succeeds_once_and_never_reopens(
    service_context: ServiceContext,
) -> None:
    auth = AuthService(service_context)
    assert auth.bootstrap_required()
    created = auth.bootstrap_administrator(
        request_id="bootstrap:test-initial",
        name="initial-admin",
    )
    token_id = created.record.id
    with pytest.raises(AuthError, match="already complete"):
        auth.bootstrap_administrator(
            request_id="bootstrap:test-conflict",
            name="second-admin",
        )

    with service_context.database.session() as session:
        token = TokenRepository(session).get_across_workspaces(token_id)
        assert token is not None
        # An administrator credential names a person, not a workspace, so deleting it
        # is the cross-scope operation rather than a tenant-scoped one.
        assert TokenRepository(session).delete_across_workspaces(token.id)

    assert not auth.bootstrap_required()
    assert auth.token_count() == 0


def test_auth_service_cache_is_explicitly_shared_reset_and_closed(
    service_context: ServiceContext,
) -> None:
    cache = AuthTokenCache()
    mutator = AuthService(service_context, token_cache=cache)
    verifier = AuthService(service_context, token_cache=cache)
    raw_token, record = mutator.create_token("shared-cache")

    assert verifier.authenticate(raw_token).id == record.id

    assert mutator.authenticate(raw_token).id == record.id

    mutator.revoke_token(record.id)
    with pytest.raises(AuthError, match="invalid token"):
        verifier.authenticate(raw_token)

    cache.close()
    cache.close()
    with pytest.raises(RuntimeError, match="auth token cache is closed"):
        verifier.authenticate(raw_token)


def test_policy_decisions_cover_workspace_admin_and_restricted_tokens(
    service_context: ServiceContext,
) -> None:
    control = ControlPlaneService(service_context)
    workspace_a = owned_workspace(control, "workspace-a")
    workspace_b = owned_workspace(control, "workspace-b")
    auth = AuthService(service_context)
    _, restricted = auth.create_token(
        "reader",
        scopes=[AuthScope.Read.value],
        kind=TokenKind.WorkspaceRestricted,
        workspace_id=workspace_a.id,
    )
    _, admin = administrator_credential(service_context, "policy-admin")

    workspace_read = workspace_requirement(workspace_a.id, action=AuthScope.Read)
    assert decide_authorization(restricted, workspace_read).allowed

    strict_decision = decide_authorization(
        restricted,
        workspace_requirement(workspace_a.id, action=AuthScope.Read, strict=True),
    )
    assert not strict_decision.allowed
    assert strict_decision.reason == AuthzDecisionReason.RestrictedToken

    wrong_workspace = decide_authorization(
        restricted,
        workspace_requirement(workspace_b.id, action=AuthScope.Read),
    )
    assert wrong_workspace.reason == AuthzDecisionReason.WrongWorkspace

    admin_decision = decide_authorization(
        admin,
        workspace_requirement(workspace_b.id, action=AuthScope.Write, strict=True),
    )
    assert admin_decision.allowed

    non_admin_decision = decide_authorization(restricted, admin_requirement())
    assert non_admin_decision.reason == AuthzDecisionReason.WrongTokenKind


def test_policy_decisions_cover_worker_and_external_input(
    service_context: ServiceContext,
) -> None:
    workspace = owned_workspace(ControlPlaneService(service_context), "workspace-a")
    auth = AuthService(service_context)
    _, public_worker = auth.create_token(
        "worker",
        scopes=[AuthScope.Worker.value, AuthScope.Machine.value],
        kind=TokenKind.Worker,
        workspace_id=workspace.id,
    )
    _, private_worker = auth.create_token(
        "private-worker",
        scopes=[AuthScope.Worker.value, AuthScope.Machine.value],
        kind=TokenKind.WorkerPrivate,
        workspace_id=workspace.id,
    )

    assert decide_authorization(private_worker, worker_requirement(private_only=True)).allowed

    public_worker_decision = decide_authorization(
        public_worker,
        worker_requirement(private_only=True),
    )
    assert public_worker_decision.reason == AuthzDecisionReason.WrongTokenKind

    policy_input = build_policy_input(
        private_worker,
        worker_requirement(private_only=True, workspace_id=workspace.id),
    )
    assert policy_input.principal is not None
    assert policy_input.principal.token_kind == TokenKind.WorkerPrivate
    assert policy_input.resource_kind == AuthzResourceKind.Worker


def test_disabled_tokens_are_rejected_by_policy(service_context: ServiceContext) -> None:
    _, record = AuthService(service_context).create_token("disabled")
    disabled = record.model_copy(update={"disabled_by_admin": True})

    decision = decide_authorization(disabled, workspace_requirement("default"))

    assert not decision.allowed
    assert decision.reason == AuthzDecisionReason.DisabledToken


def test_a_users_credential_reaches_only_the_workspaces_they_belong_to(
    service_context: ServiceContext,
) -> None:
    """Membership is the whole of a person's reach, and the role bounds what they may do.

    This is the isolation boundary the account model rests on: one customer's
    credential must not act in another customer's workspace, and a member must not
    perform an action reserved for the owner.
    """
    control = ControlPlaneService(service_context)
    users = UserService(service_context)
    me = users.create(display_name="me-user")
    them = users.create(display_name="them-user")
    mine = control.set_workspace("mine", owner_user_id=me.id)
    theirs = control.set_workspace("theirs", owner_user_id=them.id)

    _raw, my_token = AuthService(service_context).create_account_token(me.id, "cli")

    assert decide_authorization(
        my_token,
        workspace_requirement(
            mine.id,
            membership=users.membership(workspace_id=mine.id, user_id=me.id),
        ),
    ).allowed
    assert not decide_authorization(
        my_token,
        workspace_requirement(
            theirs.id,
            membership=users.membership(workspace_id=theirs.id, user_id=me.id),
        ),
    ).allowed

    # A membership row naming somebody else cannot stand in for one's own.
    assert not decide_authorization(
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
    ).allowed

    with service_context.database.session() as session:
        WorkspaceMemberRepository(session).add(
            workspace_id=theirs.id,
            user_id=me.id,
            role=WorkspaceRole.Member,
        )
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
    service_context: ServiceContext,
) -> None:
    """Automation keeps its single-workspace blast radius whatever else is presented."""
    control = ControlPlaneService(service_context)
    mine = owned_workspace(control, "mine")
    theirs = owned_workspace(control, "theirs")
    me = UserService(service_context).create(display_name="me-user")
    _raw, workspace_token = AuthService(service_context).create_token(
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
