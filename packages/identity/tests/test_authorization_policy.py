from __future__ import annotations

from contextlib import ExitStack

import identity.auth
import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.identity import TokenRepository
from fastapi.testclient import TestClient
from identity.auth import AuthError, AuthService, AuthTokenCache
from identity.authz import (
    AuthzDecisionReason,
    AuthzResourceKind,
    admin_requirement,
    build_policy_input,
    decide_authorization,
    machine_requirement,
    worker_requirement,
    workspace_requirement,
)
from shared.identity import AuthScope, AuthTokenRecord, TokenKind
from tests.service_fixtures import administrator_credential, owned_workspace


def test_auth_service_records_token_kind_and_checks_scopes(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = owned_workspace(ControlPlaneService(isolated_services.context), "workspace-a")
    cache = AuthTokenCache()
    auth = AuthService(isolated_services.context, token_cache=cache)
    stored_token_ids: list[str] = []
    original_store = cache.store

    def record_store(
        token_digest: str,
        cached_record: AuthTokenRecord,
        *,
        generation: int | None = None,
    ) -> None:
        stored_token_ids.append(cached_record.id)
        original_store(token_digest, cached_record, generation=generation)

    monkeypatch.setattr(cache, "store", record_store)
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
    assert stored_token_ids == []


def test_bootstrap_succeeds_once_and_never_reopens(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    assert client.get("/auth/bootstrap").status_code == 404
    assert client.post("/auth/bootstrap", json={"name": "initial-admin"}).status_code == 404

    auth = AuthService(isolated_services.context)
    assert auth.bootstrap_required()
    created = auth.bootstrap_administrator(
        request_id="bootstrap:test-initial",
        username="admin",
        password="bootstrap-password",
        name="initial-admin",
    )
    token_id = created.record.id
    with pytest.raises(AuthError, match="already complete"):
        auth.bootstrap_administrator(
            request_id="bootstrap:test-conflict",
            username="admin",
            password="bootstrap-password",
            name="second-admin",
        )

    with isolated_services.context.database.session() as session:
        token = TokenRepository(session).get_across_workspaces(token_id)
        assert token is not None
        # An administrator credential names a person, not a workspace, so deleting it
        # is the cross-scope operation rather than a tenant-scoped one.
        assert TokenRepository(session).delete_across_workspaces(token.id)

    assert not auth.bootstrap_required()
    assert auth.token_count() == 0


def test_auth_service_cache_is_explicitly_shared_reset_and_closed(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = AuthTokenCache()
    mutator = AuthService(isolated_services.context, token_cache=cache)
    verifier = AuthService(isolated_services.context, token_cache=cache)
    raw_token, record = mutator.create_token("shared-cache")

    assert verifier.authenticate(raw_token).id == record.id

    def fail_verify(_token: str, _encoded: str) -> bool:
        raise AssertionError("shared cache should serve the positive lookup")

    monkeypatch.setattr(identity.auth, "_verify_token", fail_verify)
    assert mutator.authenticate(raw_token).id == record.id

    mutator.revoke_token(record.id)
    with pytest.raises(AuthError, match="invalid token"):
        verifier.authenticate(raw_token)

    cache.close()
    cache.close()
    with pytest.raises(RuntimeError, match="auth token cache is closed"):
        verifier.authenticate(raw_token)


def test_auth_service_defaults_do_not_share_process_global_cache(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = AuthService(isolated_services.context)
    second = AuthService(isolated_services.context)
    raw_token, record = first.create_token("isolated-cache")

    assert first.authenticate(raw_token).id == record.id
    original_verify = identity.auth._verify_token
    verified_hashes: list[str] = []

    def record_verify(token: str, encoded: str) -> bool:
        verified_hashes.append(encoded)
        return original_verify(token, encoded)

    monkeypatch.setattr(identity.auth, "_verify_token", record_verify)

    assert second.authenticate(raw_token).id == record.id
    assert verified_hashes == [record.token_hash]


def test_policy_decisions_cover_workspace_admin_and_restricted_tokens(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace_a = owned_workspace(control, "workspace-a")
    workspace_b = owned_workspace(control, "workspace-b")
    auth = AuthService(isolated_services.context)
    _, restricted = auth.create_token(
        "reader",
        scopes=[AuthScope.Read.value],
        kind=TokenKind.WorkspaceRestricted,
        workspace_id=workspace_a.id,
    )
    _, admin = administrator_credential(isolated_services, "policy-admin")

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


def test_policy_decisions_cover_worker_machine_and_external_input(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(ControlPlaneService(isolated_services.context), "workspace-a")
    auth = AuthService(isolated_services.context)
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

    machine_decision = decide_authorization(
        private_worker,
        machine_requirement(workspace_id=workspace.id),
    )
    assert machine_decision.allowed

    policy_input = build_policy_input(
        private_worker,
        worker_requirement(private_only=True, workspace_id=workspace.id),
    )
    assert policy_input.principal is not None
    assert policy_input.principal.token_kind == TokenKind.WorkerPrivate
    assert policy_input.resource_kind == AuthzResourceKind.Worker


def test_disabled_tokens_are_rejected_by_policy(isolated_services: ApiServices) -> None:
    _, record = AuthService(isolated_services.context).create_token("disabled")
    disabled = record.model_copy(update={"disabled_by_admin": True})

    decision = decide_authorization(disabled, workspace_requirement("default"))

    assert not decision.allowed
    assert decision.reason == AuthzDecisionReason.DisabledToken
