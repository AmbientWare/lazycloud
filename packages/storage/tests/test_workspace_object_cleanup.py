from __future__ import annotations

import pytest
from control.service import ControlPlaneService
from database.context import ServiceContext
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.errors import UpstreamUnavailableError
from storage.service import ObjectStorage
from tests.fakes import FakeObjectClient
from tests.workspaces import owned_workspace


def test_object_storage_deletes_each_workspace_physical_object_independently(
    service_context: ServiceContext,
) -> None:
    object_client = FakeObjectClient()
    object_storage = ObjectStorage(service_context, object_client=object_client)
    control = ControlPlaneService(service_context)
    first = owned_workspace(control, "object-first")
    last = owned_workspace(control, "object-last")
    key = "sources/shared.zip"
    first_record = object_storage.put_bytes_for_workspace(
        workspace_id=first.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key=key,
        data=b"shared",
    )
    last_record = object_storage.put_bytes_for_workspace(
        workspace_id=last.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key=key,
        data=b"shared",
    )
    first_physical_key = object_storage.physical_key_for_record(first_record)
    last_physical_key = object_storage.physical_key_for_record(last_record)
    physical_bucket = object_storage.physical_bucket(SOURCE_PACKAGE_BUCKET)

    assert object_storage.delete_for_workspace(
        workspace_id=first.id, bucket=SOURCE_PACKAGE_BUCKET, key=key
    )
    assert not object_client.exists(first_physical_key, bucket=physical_bucket)
    assert object_client.exists(last_physical_key, bucket=physical_bucket)
    assert object_storage.list_for_workspace(workspace_id=first.id) == []
    assert (
        object_storage.get_by_id_for_workspace(last_record.id, workspace_id=last.id).id
        == last_record.id
    )

    assert object_storage.delete_for_workspace(
        workspace_id=last.id, bucket=SOURCE_PACKAGE_BUCKET, key=key
    )
    assert not object_client.exists(last_physical_key, bucket=physical_bucket)
    assert first_record.id != last_record.id


def test_object_storage_preserves_metadata_when_physical_delete_is_not_confirmed(
    service_context: ServiceContext,
) -> None:
    object_client = _StickyDeleteObjectClient()
    object_storage = ObjectStorage(service_context, object_client=object_client)
    workspace = owned_workspace(ControlPlaneService(service_context), "object-sticky")
    record = object_storage.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/sticky.zip",
        data=b"sticky",
    )
    physical_key = object_storage.physical_key_for_record(record)

    with pytest.raises(UpstreamUnavailableError, match="object deletion was not confirmed"):
        object_storage.delete_for_workspace(
            workspace_id=workspace.id, bucket=record.bucket, key=record.key
        )

    assert (
        object_storage.get_by_id_for_workspace(record.id, workspace_id=workspace.id).id == record.id
    )
    assert object_client.exists(physical_key, bucket=object_storage.physical_bucket(record.bucket))


class _StickyDeleteObjectClient(FakeObjectClient):
    def delete(self, key: str, *, bucket: str | None = None) -> None:
        _ = (key, bucket)
