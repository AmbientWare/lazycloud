from __future__ import annotations

import pytest
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.records.identity import SecretStorageRecord
from database.repositories.identity import SecretRepository
from execution.secrets.crypto import (
    SECRET_VALUE_PREFIX,
    SecretDecryptionError,
    WorkspaceSecretCipher,
)
from execution.secrets.service import SecretService
from observability.events import EventService
from shared.identity import WorkspaceRecord
from tests.workspaces import owned_workspace


def test_secrets_encrypt_before_persistence_and_read_plaintext(
    service_context: ServiceContext,
) -> None:
    secrets = SecretService(service_context, EventService(service_context))
    created = secrets.set("API_TOKEN", "stored-secret")

    secrets = SecretService(service_context, EventService(service_context))
    assert created.value == "stored-secret"
    assert secrets.get("API_TOKEN").value == "stored-secret"
    assert secrets.list()[0].value == "stored-secret"

    stored = _stored_secret(service_context, "API_TOKEN")
    assert stored.ciphertext != "stored-secret"
    assert stored.ciphertext.startswith(SECRET_VALUE_PREFIX)


def test_secret_ciphertext_is_workspace_bound(
    service_context: ServiceContext,
) -> None:
    workspace_a = _workspace(service_context, "workspace-a")
    workspace_b = _workspace(service_context, "workspace-b")
    encrypted = WorkspaceSecretCipher.from_workspace(workspace_a).encrypt("API_TOKEN", "secret-a")

    assert (
        WorkspaceSecretCipher.from_workspace(workspace_a).decrypt("API_TOKEN", encrypted)
        == "secret-a"
    )
    with pytest.raises(SecretDecryptionError):
        WorkspaceSecretCipher.from_workspace(workspace_b).decrypt("API_TOKEN", encrypted)


def test_secrets_are_workspace_scoped(service_context: ServiceContext) -> None:
    secrets = SecretService(service_context, EventService(service_context))
    workspace_a = _workspace(service_context, "workspace-a")
    workspace_b = _workspace(service_context, "workspace-b")

    secrets.set("API_TOKEN", "secret-a", workspace=workspace_a.id)
    secrets.set("API_TOKEN", "secret-b", workspace=workspace_b.name)

    assert secrets.get("API_TOKEN", workspace=workspace_a.name).value == "secret-a"
    assert secrets.get("API_TOKEN", workspace=workspace_b.id).value == "secret-b"
    assert [item.value for item in secrets.list(workspace=workspace_a.id)] == ["secret-a"]


def test_secret_plaintext_records_fail_closed_without_persistence_write(
    service_context: ServiceContext,
) -> None:
    secrets = SecretService(service_context, EventService(service_context))
    with service_context.database.session() as session:
        workspace = service_context.workspace(session)
        stored = SecretRepository(session).create(
            "PLAINTEXT_TOKEN",
            "plaintext-is-not-current-ciphertext",
            workspace_id=workspace.id,
        )

    with pytest.raises(SecretDecryptionError, match="current encrypted format"):
        secrets.get("PLAINTEXT_TOKEN")

    retained = _stored_secret(service_context, "PLAINTEXT_TOKEN")
    assert retained.id == stored.id
    assert retained.ciphertext == "plaintext-is-not-current-ciphertext"
    assert retained.updated_at == stored.updated_at


def test_secret_tampering_is_rejected(service_context: ServiceContext) -> None:
    secrets = SecretService(service_context, EventService(service_context))
    secrets.set("API_TOKEN", "stored-secret")
    stored = _stored_secret(service_context, "API_TOKEN")
    tampered_value = _tamper(stored.ciphertext)
    with service_context.database.session() as session:
        workspace = service_context.workspace(session)
        SecretRepository(session).update(
            stored.name,
            tampered_value,
            workspace_id=workspace.id,
        )

    with pytest.raises(SecretDecryptionError):
        secrets.get("API_TOKEN")


@pytest.mark.parametrize("ciphertext", [f"{SECRET_VALUE_PREFIX}!bad!", SECRET_VALUE_PREFIX])
def test_malformed_secret_ciphertext_is_rejected(
    service_context: ServiceContext,
    ciphertext: str,
) -> None:
    secrets = SecretService(service_context, EventService(service_context))
    with service_context.database.session() as session:
        workspace = service_context.workspace(session)
        SecretRepository(session).create(
            "MALFORMED_TOKEN",
            ciphertext,
            workspace_id=workspace.id,
        )

    with pytest.raises(SecretDecryptionError):
        secrets.get("MALFORMED_TOKEN")


def test_secret_ciphertext_is_bound_to_its_stored_name(
    service_context: ServiceContext,
) -> None:
    secrets = SecretService(service_context, EventService(service_context))
    with service_context.database.session() as session:
        workspace = service_context.workspace(session)
        cipher = WorkspaceSecretCipher.from_workspace(workspace)
        SecretRepository(session).create(
            "RENAMED_TOKEN",
            cipher.encrypt("ORIGINAL_TOKEN", "secret"),
            workspace_id=workspace.id,
        )

    with pytest.raises(SecretDecryptionError, match="failed authentication"):
        secrets.get("RENAMED_TOKEN")


def test_secret_reads_do_not_rewrite_ciphertext_or_timestamps(
    service_context: ServiceContext,
) -> None:
    secrets = SecretService(service_context, EventService(service_context))
    secrets.set("READ_ONLY", "secret")
    before = _stored_secret(service_context, "READ_ONLY")

    assert secrets.get("READ_ONLY").value == "secret"
    assert secrets.list()[0].value == "secret"

    after = _stored_secret(service_context, "READ_ONLY")
    assert after == before


def _stored_secret(context: ServiceContext, name: str) -> SecretStorageRecord:
    with context.database.session() as session:
        workspace = context.workspace(session)
        record = SecretRepository(session).get(name, workspace_id=workspace.id)
    assert record is not None
    return record


def _workspace(context: ServiceContext, name: str) -> WorkspaceRecord:
    return owned_workspace(ControlPlaneService(context), name)


def _tamper(value: str) -> str:
    index = len(SECRET_VALUE_PREFIX) + 5
    replacement = "A" if value[index] != "A" else "B"
    return f"{value[:index]}{replacement}{value[index + 1 :]}"
