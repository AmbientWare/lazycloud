from __future__ import annotations

from control.service import ControlPlaneService
from database.context import ServiceContext
from storage.service import ObjectStorage
from tests.fakes import FakeObjectClient
from tests.workspaces import owned_workspace


def test_workspace_object_cleanup_preserves_external_bucket_data(
    service_context: ServiceContext,
) -> None:
    workspace = owned_workspace(ControlPlaneService(service_context), "cleanup-tenant")
    object_client = FakeObjectClient()
    storage = ObjectStorage(
        service_context,
        object_client=object_client,
        default_bucket="owned",
    )
    storage.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket="owned",
        key="artifacts/task/result.txt",
        data=b"owned-data",
    )
    object_client.put_bytes(
        "customer/preserved.txt",
        b"external-data",
        bucket="external",
    )

    assert storage.delete_workspace_objects(workspace.id) == 1
    assert ("owned", "artifacts/task/result.txt") not in object_client.objects
    assert object_client.objects[("external", "customer/preserved.txt")] == b"external-data"
