from datetime import UTC, datetime

from database.context import ServiceContext
from database.repositories.identity import (
    UserRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from database.tables.storage_access import StorageAccessTable
from shared.identity import WorkspaceStorageConfig
from shared.storage_access import (
    StorageAccessObservation,
    StorageRequestClass,
    StorageTransferEvidence,
)
from sqlalchemy import select
from storage.access_metering import record_storage_access
from storage_client.s3 import S3ObjectStoreSettings


def test_storage_observations_dedupe_survive_deletion_and_exclude_customer_storage(
    service_context: ServiceContext,
) -> None:
    database = service_context.database
    settings = S3ObjectStoreSettings(
        bucket="platform-objects",
        workspace_bucket_prefix="platform-workspace",
        endpoint_url="https://s3.us-east-1.amazonaws.com",
        region_name="us-east-1",
    )
    with database.session() as session:
        user = UserRepository(session).create(display_name="storage accounting")
        workspace = WorkspaceRepository(session).create(name="storage accounting")
        WorkspaceMemberRepository(session).ensure_owner(workspace_id=workspace.id, user_id=user.id)
        external = WorkspaceRepository(session).create(name="customer storage")
        external.storage = WorkspaceStorageConfig(
            backend="s3",
            bucket=f"platform-workspace-{external.id}",
            config={
                "endpoint_url": settings.endpoint_url,
                "region": "us-east-1",
                "access_key": "customer-key",
                "secret_key": "customer-secret",
            },
        )
        WorkspaceRepository(session).upsert(external)
    observation = StorageAccessObservation(
        provider="aws",
        bucket=settings.bucket,
        key=f"workspaces/{workspace.id}/artifacts/file",
        request_id="request-one",
        operation="REST.GET.OBJECT",
        request_class=StorageRequestClass.Read,
        occurred_at=datetime.now(UTC),
        status_code=206,
        response_bytes=4096,
        source_region="",
        transfer_evidence=StorageTransferEvidence.Unknown,
    )
    external_observation = observation.model_copy(
        update={"bucket": external.storage.bucket, "request_id": "customer-request"}
    )
    assert record_storage_access(database, settings, (observation, external_observation)) == 2
    assert record_storage_access(database, settings, (observation,)) == 0
    with database.session() as session:
        repository = WorkspaceRepository(session)
        active = repository.get(workspace.id)
        assert active is not None
        deleting = repository.mark_deleting(active)
        repository.purge_owned_records(deleting.id)
        repository.tombstone(deleting)
    late = observation.model_copy(update={"request_id": "late-request"})
    assert record_storage_access(database, settings, (late,)) == 1
    assert record_storage_access(database, settings, (observation,)) == 0
    with database.session() as session:
        rows = {row.request_id: row for row in session.scalars(select(StorageAccessTable)).all()}
        assert len(rows) == 3
        assert rows["request-one"].workspace_id == workspace.id
        assert rows["request-one"].response_bytes == 4096
        assert rows["customer-request"].workspace_id is None
        assert rows["late-request"].workspace_id == workspace.id
