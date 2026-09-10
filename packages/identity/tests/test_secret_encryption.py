from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.records.identity import SecretStorageRecord
from database.repositories.identity import SecretRepository
from execution.secrets.crypto import (
    SECRET_VALUE_PREFIX,
    SecretDecryptionError,
    WorkspaceSecretCipher,
)
from shared.identity import WorkspaceRecord
from tests.workspaces import owned_workspace


def test_secrets_encrypt_before_persistence_and_read_plaintext(
    isolated_services: ApiServices,
) -> None:
    created = isolated_services.secrets.set("API_TOKEN", "stored-secret")

    assert created.value == "stored-secret"
    assert isolated_services.secrets.get("API_TOKEN").value == "stored-secret"
    assert isolated_services.secrets.list()[0].value == "stored-secret"

    stored = _stored_secret(isolated_services, "API_TOKEN")
    assert stored.ciphertext != "stored-secret"
    assert stored.ciphertext.startswith(SECRET_VALUE_PREFIX)


def test_secret_ciphertext_is_workspace_bound(
    isolated_services: ApiServices,
) -> None:
    workspace_a = _workspace(isolated_services, "workspace-a")
    workspace_b = _workspace(isolated_services, "workspace-b")
    encrypted = WorkspaceSecretCipher.from_workspace(workspace_a).encrypt("API_TOKEN", "secret-a")

    assert (
        WorkspaceSecretCipher.from_workspace(workspace_a).decrypt("API_TOKEN", encrypted)
        == "secret-a"
    )
    with pytest.raises(SecretDecryptionError):
        WorkspaceSecretCipher.from_workspace(workspace_b).decrypt("API_TOKEN", encrypted)


def test_secrets_are_workspace_scoped(isolated_services: ApiServices) -> None:
    workspace_a = _workspace(isolated_services, "workspace-a")
    workspace_b = _workspace(isolated_services, "workspace-b")

    isolated_services.secrets.set("API_TOKEN", "secret-a", workspace=workspace_a.id)
    isolated_services.secrets.set("API_TOKEN", "secret-b", workspace=workspace_b.name)

    assert (
        isolated_services.secrets.get("API_TOKEN", workspace=workspace_a.name).value == "secret-a"
    )
    assert isolated_services.secrets.get("API_TOKEN", workspace=workspace_b.id).value == "secret-b"
    assert [item.value for item in isolated_services.secrets.list(workspace=workspace_a.id)] == [
        "secret-a"
    ]


def test_secret_plaintext_records_fail_closed_without_persistence_write(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.database.session() as session:
        workspace = isolated_services.context.workspace(session)
        stored = SecretRepository(session).create(
            "PLAINTEXT_TOKEN",
            "plaintext-is-not-current-ciphertext",
            workspace_id=workspace.id,
        )

    with pytest.raises(SecretDecryptionError, match="current encrypted format"):
        isolated_services.secrets.get("PLAINTEXT_TOKEN")

    retained = _stored_secret(isolated_services, "PLAINTEXT_TOKEN")
    assert retained.id == stored.id
    assert retained.ciphertext == "plaintext-is-not-current-ciphertext"
    assert retained.updated_at == stored.updated_at


def test_secret_tampering_is_rejected(isolated_services: ApiServices) -> None:
    isolated_services.secrets.set("API_TOKEN", "stored-secret")
    stored = _stored_secret(isolated_services, "API_TOKEN")
    tampered_value = _tamper(stored.ciphertext)
    with isolated_services.database.session() as session:
        workspace = isolated_services.context.workspace(session)
        SecretRepository(session).update(
            stored.name,
            tampered_value,
            workspace_id=workspace.id,
        )

    with pytest.raises(SecretDecryptionError):
        isolated_services.secrets.get("API_TOKEN")


@pytest.mark.parametrize("ciphertext", [f"{SECRET_VALUE_PREFIX}!bad!", SECRET_VALUE_PREFIX])
def test_malformed_secret_ciphertext_is_rejected(
    isolated_services: ApiServices,
    ciphertext: str,
) -> None:
    with isolated_services.database.session() as session:
        workspace = isolated_services.context.workspace(session)
        SecretRepository(session).create(
            "MALFORMED_TOKEN",
            ciphertext,
            workspace_id=workspace.id,
        )

    with pytest.raises(SecretDecryptionError):
        isolated_services.secrets.get("MALFORMED_TOKEN")


def test_secret_ciphertext_is_bound_to_its_stored_name(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.database.session() as session:
        workspace = isolated_services.context.workspace(session)
        cipher = WorkspaceSecretCipher.from_workspace(workspace)
        SecretRepository(session).create(
            "RENAMED_TOKEN",
            cipher.encrypt("ORIGINAL_TOKEN", "secret"),
            workspace_id=workspace.id,
        )

    with pytest.raises(SecretDecryptionError, match="failed authentication"):
        isolated_services.secrets.get("RENAMED_TOKEN")


def test_secret_reads_do_not_rewrite_ciphertext_or_timestamps(
    isolated_services: ApiServices,
) -> None:
    isolated_services.secrets.set("READ_ONLY", "secret")
    before = _stored_secret(isolated_services, "READ_ONLY")

    assert isolated_services.secrets.get("READ_ONLY").value == "secret"
    assert isolated_services.secrets.list()[0].value == "secret"

    after = _stored_secret(isolated_services, "READ_ONLY")
    assert after == before


def _stored_secret(services: ApiServices, name: str) -> SecretStorageRecord:
    with services.database.session() as session:
        workspace = services.context.workspace(session)
        record = SecretRepository(session).get(name, workspace_id=workspace.id)
    assert record is not None
    return record


def _workspace(services: ApiServices, name: str) -> WorkspaceRecord:
    return owned_workspace(ControlPlaneService(services.context), name)


def _tamper(value: str) -> str:
    index = len(SECRET_VALUE_PREFIX) + 5
    replacement = "A" if value[index] != "A" else "B"
    return f"{value[:index]}{replacement}{value[index + 1 :]}"
