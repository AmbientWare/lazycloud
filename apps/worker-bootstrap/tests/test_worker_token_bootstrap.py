from __future__ import annotations

import stat
from pathlib import Path

import pytest
from database.repositories.identity import TokenRepository
from identity.auth import AuthService, IdentityDatabaseContext
from identity.platform import PlatformNamespaceService
from shared.errors import NotFoundError
from shared.identity import TokenKind
from worker_bootstrap_app.main import write_worker_token

from database import DatabaseClient


def test_worker_token_requires_platform_initialization_without_a_human_account(
    database: DatabaseClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = database.settings.url
    monkeypatch.setenv("LAZYCLOUD_DATABASE_URL", database_url)
    output = tmp_path / "worker-token"

    with pytest.raises(NotFoundError, match="platform namespace is not initialized"):
        write_worker_token(name="compose-container-worker", output=output)
    assert not output.exists()
    auth = AuthService(IdentityDatabaseContext(database))
    assert auth.bootstrap_required()
    namespace = PlatformNamespaceService(database).initialize()
    assert PlatformNamespaceService(database).initialize() == namespace

    write_worker_token(name="compose-container-worker", output=output)
    first = output.read_text(encoding="utf-8").strip()
    write_worker_token(name="compose-container-worker", output=output)
    second = output.read_text(encoding="utf-8").strip()

    assert first == second
    record = auth.validate_service_token(
        second,
        name="compose-container-worker",
        kind=TokenKind.Worker,
        workspace_id=namespace.id,
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
    assert auth.bootstrap_required()
