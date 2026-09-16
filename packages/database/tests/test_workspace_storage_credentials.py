from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from database.tables.identity import WorkspaceTable
from shared.identity import WorkspaceStorageConfig


def test_external_storage_credentials_survive_revocation_until_cleanup(
    service_context: ServiceContext,
) -> None:
    with service_context.database.session() as session:
        repository = WorkspaceRepository(session)
        workspace = repository.create(name="encrypted-storage")
        workspace.storage = WorkspaceStorageConfig(
            backend="s3",
            bucket="customer-bucket",
            config={
                "endpoint_url": "https://storage.example.test",
                "access_key": "synthetic-access",
                "secret_key": "synthetic-secret",
            },
        )
        repository.upsert(workspace)
        row = session.get(WorkspaceTable, workspace.id)
        assert row is not None
        assert row.storage_access_key_ciphertext not in (None, "synthetic-access")
        assert row.storage_secret_key_ciphertext not in (None, "synthetic-secret")

    with service_context.database.session() as session:
        repository = WorkspaceRepository(session)
        workspace = repository.get(workspace.id)
        assert workspace is not None
        assert workspace.storage.access_key == "synthetic-access"
        assert workspace.storage.secret_key == "synthetic-secret"
        deleting = repository.mark_deleting(workspace)
        assert deleting.signing_key == ""
        assert deleting.storage.secret_key == "synthetic-secret"

    with service_context.database.session() as session:
        repository = WorkspaceRepository(session)
        workspace = repository.get(deleting.id)
        assert workspace is not None
        assert workspace.storage.secret_key == "synthetic-secret"
        deleted = repository.tombstone(workspace)
        assert deleted.storage.secret_key == ""
        row = session.get(WorkspaceTable, deleted.id)
        assert row is not None
        assert row.storage_credential_key is None
        assert row.storage_access_key_ciphertext is None
        assert row.storage_secret_key_ciphertext is None
