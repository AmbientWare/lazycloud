from __future__ import annotations

import stat
from pathlib import Path

import pytest
from database.repositories.identity import TokenRepository
from identity.auth import AuthError, AuthService, IdentityDatabaseContext
from shared.identity import TokenKind
from worker_bootstrap_app.main import write_worker_token

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_worker_token_waits_for_admin_and_retries_without_leaking_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'worker-token.db'}"
    monkeypatch.setenv("LAZYCLOUD_DATABASE_URL", database_url)
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Test)
    )
    database.create_schema()
    output = tmp_path / "worker-token"

    with pytest.raises(AuthError, match="bootstrap must complete"):
        write_worker_token(name="compose-container-worker", output=output)
    assert not output.exists()
    auth = AuthService(IdentityDatabaseContext(database))
    assert auth.bootstrap_required()
    auth.bootstrap_administrator(request_id="bootstrap:worker-token-test")
    with pytest.raises(AuthError, match="bootstrap must complete"):
        write_worker_token(name="compose-container-worker", output=output)
    auth.mark_admin_token_published(
        request_id="bootstrap:worker-token-test",
        recovery=False,
    )

    write_worker_token(name="compose-container-worker", output=output)
    first = output.read_text(encoding="utf-8").strip()
    write_worker_token(name="compose-container-worker", output=output)
    second = output.read_text(encoding="utf-8").strip()

    assert first == second
    record = auth.validate_service_token(
        second,
        name="compose-container-worker",
        kind=TokenKind.Worker,
        workspace_id="default",
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert list(tmp_path.glob("*.pending")) == []
    with database.session() as session:
        owned = TokenRepository(session).list_owned_credentials(
            workspace_id=record.workspace_id,
            name="compose-container-worker",
            kind=TokenKind.Worker,
        )
    assert [item.id for item in owned] == [record.id]
    database.dispose()
