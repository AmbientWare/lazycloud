from __future__ import annotations

import hmac
import stat
from pathlib import Path
from secrets import token_urlsafe

import pytest
from api.server.services import ApiServices
from database.repositories.identity import TokenRepository, WorkspaceAuditRepository
from identity.auth import AuthError, AuthService
from identity.credential_files import CredentialFileError, CredentialFilePublication
from shared.http.workspaces import WorkspaceAuditAction
from shared.identity import TokenKind


def _configured_token() -> str:
    return f"rt_{token_urlsafe(32)}"


def test_credential_publication_refuses_a_symlink_output(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.write_text("do-not-touch\n", encoding="utf-8")
    output = tmp_path / "admin-token"
    output.symlink_to(protected)
    publication = CredentialFilePublication(output, "bootstrap:symlink-test")

    with pytest.raises(CredentialFileError, match="not a regular file"):
        publication.read_published()

    assert protected.read_text(encoding="utf-8") == "do-not-touch\n"


def test_service_credentials_cannot_consume_or_bypass_admin_claim(
    isolated_services: ApiServices,
) -> None:
    auth = AuthService(isolated_services.context)

    auth.create_token("legacy-internal", kind=TokenKind.Worker)

    assert auth.bootstrap_required()
    with pytest.raises(AuthError, match="bootstrap must complete"):
        auth.create_service_token("container-worker", kind=TokenKind.Worker)


def test_bootstrap_retry_publishes_the_exact_committed_token_once(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    auth = AuthService(isolated_services.context)
    publication = CredentialFilePublication(
        tmp_path / "admin-token",
        "bootstrap:test-publication",
    )
    created = auth.bootstrap_administrator(
        request_id="bootstrap:test-publication",
        stage_token=publication.stage,
    )
    staged = publication.read_staged()

    assert staged == created.token
    assert publication.read_published() is None
    replay = auth.bootstrap_administrator(
        request_id="bootstrap:test-publication",
        staged_token=staged,
    )
    publication.publish(replace=False)
    auth.mark_admin_token_published(
        request_id="bootstrap:test-publication",
        recovery=False,
    )

    assert replay.replayed
    assert replay.record.id == created.record.id
    assert publication.read_published() == created.token
    assert stat.S_IMODE(publication.resolved_output.stat().st_mode) == 0o600
    assert publication.read_staged() is None
    with isolated_services.context.database.session() as session:
        tokens = TokenRepository(session).list_across_workspaces()
        # The audit belongs to the workspace bootstrap created, not to the credential:
        # an administrator credential names a person and carries no workspace.
        audits = WorkspaceAuditRepository(session).page(
            workspace_id=isolated_services.context.workspace(session, "default").id,
            limit=20,
        )
    assert [token.id for token in tokens if token.kind is TokenKind.Admin] == [created.record.id]
    assert [
        audit.action
        for audit in audits.records
        if audit.action is WorkspaceAuditAction.AdministratorBootstrapped
    ] == [WorkspaceAuditAction.AdministratorBootstrapped]


def test_configured_bootstrap_token_is_stable_and_only_its_hash_is_stored(
    isolated_services: ApiServices,
) -> None:
    auth = AuthService(isolated_services.context)
    configured = _configured_token()

    created = auth.bootstrap_administrator(
        request_id="bootstrap:configured-authority",
        configured_token=configured,
    )
    replay = auth.bootstrap_administrator(
        request_id="bootstrap:configured-authority",
        configured_token=configured,
    )

    assert hmac.compare_digest(created.token, configured)
    assert replay.replayed
    assert replay.record.id == created.record.id
    assert auth.authenticate(configured).id == created.record.id
    with isolated_services.context.database.session() as session:
        stored = TokenRepository(session).get_across_workspaces(created.record.id)
    assert stored is not None
    assert configured not in stored.token_hash
    assert stored.token_hash.startswith("pbkdf2_sha256$")


def test_configured_bootstrap_token_mismatch_fails_closed(
    isolated_services: ApiServices,
) -> None:
    auth = AuthService(isolated_services.context)
    configured = _configured_token()
    created = auth.bootstrap_administrator(
        request_id="bootstrap:configured-mismatch",
        configured_token=configured,
    )

    with pytest.raises(AuthError, match="retry with its staged output"):
        auth.bootstrap_administrator(
            request_id="bootstrap:configured-mismatch",
            configured_token=_configured_token(),
        )

    assert auth.authenticate(configured).id == created.record.id


def test_configured_bootstrap_token_requires_canonical_token(
    isolated_services: ApiServices,
) -> None:
    auth = AuthService(isolated_services.context)

    with pytest.raises(AuthError, match="configured administrator credential is invalid"):
        auth.bootstrap_administrator(
            request_id="bootstrap:configured-invalid",
            configured_token=token_urlsafe(32),
        )

    assert auth.bootstrap_required()


def test_recovery_request_replay_is_idempotent_and_audited_once(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    auth = AuthService(isolated_services.context)
    auth.bootstrap_administrator(request_id="bootstrap:test-recovery-owner")
    publication = CredentialFilePublication(
        tmp_path / "recovery-token",
        "recovery:incident-2026-07-19",
    )
    created = auth.recover_admin_token(
        request_id="incident-2026-07-19",
        stage_token=publication.stage,
    )
    staged = publication.read_staged()
    assert staged is not None

    replay = auth.recover_admin_token(
        request_id="incident-2026-07-19",
        staged_token=staged,
    )
    publication.publish(replace=False)
    auth.mark_admin_token_published(
        request_id="incident-2026-07-19",
        recovery=True,
    )

    assert replay.replayed
    assert replay.record.id == created.record.id
    with isolated_services.context.database.session() as session:
        audits = WorkspaceAuditRepository(session).page(
            workspace_id=isolated_services.context.workspace(session, "default").id,
            limit=20,
        )
    recovered = [
        audit
        for audit in audits.records
        if audit.action is WorkspaceAuditAction.AdministratorRecovered
    ]
    assert len(recovered) == 1
    assert recovered[0].new_value == "incident-2026-07-19"


def test_different_bootstrap_request_cannot_replay_committed_claim(
    isolated_services: ApiServices,
) -> None:
    auth = AuthService(isolated_services.context)
    auth.bootstrap_administrator(request_id="bootstrap:test-first-request")

    with pytest.raises(AuthError, match="already complete"):
        auth.bootstrap_administrator(
            request_id="bootstrap:test-other-request",
        )
