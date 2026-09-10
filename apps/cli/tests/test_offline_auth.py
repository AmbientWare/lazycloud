from __future__ import annotations

import os
import stat
from pathlib import Path
from secrets import token_urlsafe

import pytest
from botocore.exceptions import ClientError
from cli.main import build_admin_cli
from cli.offline_auth import _read_configured_token
from control.service import WorkspaceStorageError
from identity.auth import AuthService, IdentityDatabaseContext
from identity.credential_files import CredentialFileError
from shared.errors import ConflictError
from shared.identity import TokenKind
from storage_client.s3 import S3ObjectStoreClient
from typer.testing import CliRunner

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

cli = build_admin_cli()


def _token() -> str:
    return f"rt_{token_urlsafe(32)}"


def _refuse_bucket_creation(self: S3ObjectStoreClient, bucket: str | None = None) -> None:
    raise ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "storage is unavailable"}},
        "CreateBucket",
    )


def test_offline_bootstrap_publishes_a_private_credential_and_fails_loudly_without_storage(
    database: DatabaseClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap never reports success when it cannot provision workspace storage.

    The command creates the administrator credential and then provisions the
    bootstrap workspace's bucket. Storage refuses the bucket creation. The
    credential is published privately, no secret reaches stdout
    or the raised error, and the failure surfaces with its reason instead of a
    payload claiming the workspace is ready. The success path's exact-once
    replay is proven by the identity owner.
    """
    database_url = database.settings.url
    monkeypatch.setattr(S3ObjectStoreClient, "create_bucket", _refuse_bucket_creation)
    monkeypatch.setenv("LAZYCLOUD_DATABASE_URL", database_url)
    monkeypatch.setenv("LAZYCLOUD_DATABASE_DIRECT_URL", database_url)
    output = tmp_path / "admin-token"

    created = CliRunner().invoke(
        cli,
        [
            "--json",
            "auth",
            "bootstrap",
            "--output",
            str(output),
        ],
    )

    assert created.exit_code == 1
    assert isinstance(created.exception, WorkspaceStorageError)
    assert "unable to create workspace storage bucket" in str(created.exception)
    assert "rt_" not in created.output
    assert "rt_" not in str(created.exception)
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
    assert record.kind is TokenKind.Admin


def test_offline_recovery_refuses_non_postgresql_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'recovery.db'}"
    monkeypatch.setenv("LAZYCLOUD_DATABASE_URL", database_url)
    monkeypatch.setenv("LAZYCLOUD_DATABASE_DIRECT_URL", database_url)
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


@pytest.mark.parametrize("source", ["file", "environment"])
def test_offline_bootstrap_preserves_configured_credential_when_storage_fails(
    database: DatabaseClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    database_url = database.settings.url
    monkeypatch.setattr(S3ObjectStoreClient, "create_bucket", _refuse_bucket_creation)
    monkeypatch.setenv("LAZYCLOUD_DATABASE_URL", database_url)
    monkeypatch.setenv("LAZYCLOUD_DATABASE_DIRECT_URL", database_url)
    configured = _token()
    token_file = tmp_path / "configured-token"
    token_file.write_text(f"{configured}\n", encoding="utf-8")
    os.chmod(token_file, 0o400)
    output = tmp_path / "administrator-token"
    if source == "environment":
        monkeypatch.setenv("LAZYCLOUD_TOKEN", configured)
    credential_args = ["--token-file", str(token_file)] if source == "file" else []

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "auth",
            "bootstrap",
            *credential_args,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, WorkspaceStorageError)
    assert configured not in result.output
    assert configured not in str(result.exception)
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


def test_bootstrap_rejects_conflicting_credentials_without_disclosing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_token = _token()
    file_token = _token()
    monkeypatch.setenv("LAZYCLOUD_TOKEN", environment_token)
    token_file = tmp_path / "administrator-token"
    token_file.write_text(file_token, encoding="utf-8")
    token_file.chmod(0o600)

    result = CliRunner().invoke(cli, ["auth", "bootstrap", "--token-file", str(token_file)])

    assert result.exit_code == 1
    assert isinstance(result.exception, CredentialFileError)
    assert "different credentials" in str(result.exception)
    for token in (environment_token, file_token):
        assert token not in result.output
        assert token not in str(result.exception)


def test_configured_token_file_requires_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "configured-token"
    path.write_text(f"{_token()}\n", encoding="utf-8")
    os.chmod(path, 0o644)

    with pytest.raises(CredentialFileError, match="group/world"):
        _read_configured_token(path)

    os.chmod(path, 0o600)
    assert _read_configured_token(path) is not None
