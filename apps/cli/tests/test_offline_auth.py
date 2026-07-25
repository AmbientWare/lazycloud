from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from secrets import token_urlsafe

import pytest
from cli.main import build_admin_cli
from cli.offline_auth import _read_configured_token
from identity.auth import AuthService, IdentityDatabaseContext
from identity.credential_files import CredentialFileError
from shared.errors import ConflictError
from shared.identity import TokenKind
from typer.testing import CliRunner

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

cli = build_admin_cli()


def _token() -> str:
    return f"rt_{token_urlsafe(32)}"


def test_offline_bootstrap_publishes_once_without_printing_the_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'bootstrap.db'}"
    monkeypatch.setenv("LAZYCLOUD_DATABASE_URL", database_url)
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Test)
    )
    database.create_schema()
    database.dispose()
    output = tmp_path / "admin-token"
    runner = CliRunner()

    created = runner.invoke(
        cli,
        ["--json", "auth", "bootstrap", "--output", str(output)],
    )
    replayed = runner.invoke(
        cli,
        ["--json", "auth", "bootstrap", "--output", str(output)],
    )

    assert created.exit_code == 0, created.output
    assert replayed.exit_code == 0, replayed.output
    assert "rt_" not in created.output
    assert "rt_" not in replayed.output
    created_payload = json.loads(created.output)
    replayed_payload = json.loads(replayed.output)
    assert created_payload["status"] == "created"
    assert replayed_payload["status"] == "already_published"
    assert replayed_payload["token_id"] == created_payload["token_id"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert list(tmp_path.glob("*.pending")) == []
    token = output.read_text(encoding="utf-8").strip()
    verification_database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Test)
    )
    try:
        record = AuthService(IdentityDatabaseContext(verification_database)).authenticate(token)
    finally:
        verification_database.dispose()
    assert record.id == created_payload["token_id"]


def test_offline_recovery_refuses_non_postgresql_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'recovery.db'}"
    monkeypatch.setenv("LAZYCLOUD_DATABASE_URL", database_url)
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Test)
    )
    database.create_schema()
    database.dispose()
    output = tmp_path / "recovery-token"

    result = CliRunner().invoke(
        cli,
        [
            "auth",
            "recover",
            "--request-id",
            "incident-test-001",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, ConflictError)
    assert "requires PostgreSQL" in str(result.exception)
    assert "rt_" not in result.output
    assert not output.exists()


def test_offline_bootstrap_accepts_configured_token_only_through_private_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'configured-bootstrap.db'}"
    monkeypatch.setenv("LAZYCLOUD_DATABASE_URL", database_url)
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Test)
    )
    database.create_schema()
    database.dispose()
    configured = _token()
    token_file = tmp_path / "configured-token"
    token_file.write_text(f"{configured}\n", encoding="utf-8")
    os.chmod(token_file, 0o400)
    output = tmp_path / "administrator-token"

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "auth",
            "bootstrap",
            "--token-file",
            str(token_file),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "rt_" not in result.output
    assert not output.exists()
    verification_database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Test)
    )
    try:
        record = AuthService(IdentityDatabaseContext(verification_database)).authenticate(
            configured
        )
    finally:
        verification_database.dispose()
    assert record.kind is TokenKind.Admin


def test_configured_token_file_requires_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "configured-token"
    path.write_text(f"{_token()}\n", encoding="utf-8")
    os.chmod(path, 0o644)

    with pytest.raises(CredentialFileError, match="group/world"):
        _read_configured_token(path)

    os.chmod(path, 0o600)
    assert _read_configured_token(path) is not None


def test_configured_token_file_requires_canonical_token(tmp_path: Path) -> None:
    path = tmp_path / "configured-token"
    path.write_text(f"configured_{token_urlsafe(32)}\n", encoding="utf-8")
    os.chmod(path, 0o600)

    with pytest.raises(CredentialFileError, match="valid token"):
        _read_configured_token(path)
