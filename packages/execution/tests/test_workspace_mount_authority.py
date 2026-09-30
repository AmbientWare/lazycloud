from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from control.service import ControlServices
from database.context import ServiceContext
from execution.mounts import (
    container_resource_mounts,
    container_resource_mounts_require_workspace_storage,
    devbox_artifact_mount,
    platform_volume_local_path,
    source_code_mounts,
)
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.errors import UpstreamUnavailableError
from shared.identity import WorkspaceStorageConfig
from storage.service import ObjectStorage
from tests.fakes import FakeObjectClient
from tests.workspaces import owned_workspace


def test_source_code_mounts_presign_for_the_source_workspace(
    service_context: ServiceContext,
) -> None:
    object_client = FakeObjectClient()
    object_storage = ObjectStorage(service_context, object_client=object_client)
    workspace = owned_workspace(
        ControlServices.create(service_context),
        "source-owner",
    )
    record = object_storage.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/cross-workspace.zip",
        data=b"source archive",
        content_type="application/zip",
    )

    mounts = source_code_mounts(
        context=service_context,
        object_storage=object_storage,
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        object_id=record.id,
    )
    physical_key = object_storage.physical_key_for_record(record)
    physical_bucket = object_storage.physical_bucket(SOURCE_PACKAGE_BUCKET)

    selected = urlsplit(mounts[0].source_download_url)
    assert selected.netloc == physical_bucket
    assert selected.path == f"/{physical_key}"
    foreign = owned_workspace(
        ControlServices.create(service_context),
        "other-source-owner",
    )
    assert (
        source_code_mounts(
            context=service_context,
            object_storage=object_storage,
            workspace_id=foreign.id,
            workspace_name=foreign.name,
            object_id=record.id,
        )
        == []
    )


def test_container_resource_mounts_require_workspace_storage_when_workspace_has_bucket(
    service_context: ServiceContext,
) -> None:
    control = ControlServices.create(service_context)
    unprovisioned = owned_workspace(control, "mount-storage-unprovisioned")
    mounts = container_resource_mounts(
        context=service_context,
        object_storage=ObjectStorage(service_context, object_client=FakeObjectClient()),
        workspace_id=unprovisioned.id,
        workspace_name=unprovisioned.name,
        object_id="",
        container_id="ctr-1",
        volumes=[],
    )

    assert not container_resource_mounts_require_workspace_storage(
        workspace=unprovisioned, mounts=mounts
    )
    mounts.append(devbox_artifact_mount(workspace_name=unprovisioned.name, stub_id="stub-1"))
    with pytest.raises(UpstreamUnavailableError, match="no storage provisioned"):
        container_resource_mounts_require_workspace_storage(
            workspace=unprovisioned,
            mounts=mounts,
        )

    control.workspaces.set_workspace_storage(
        unprovisioned.id,
        WorkspaceStorageConfig(
            backend="s3",
            bucket="workspace-bucket",
            endpoint_url="http://object-store:9000",
            region="us-east-1",
        ),
    )

    assert container_resource_mounts_require_workspace_storage(
        workspace=control.workspaces.get_workspace(unprovisioned.id),
        mounts=mounts,
    )

    volume_only_mounts = container_resource_mounts(
        context=service_context,
        object_storage=ObjectStorage(service_context, object_client=FakeObjectClient()),
        workspace_id=unprovisioned.id,
        workspace_name=unprovisioned.name,
        object_id="",
        container_id="ctr-volume-only",
        volumes=[{"id": "canonical", "mount_path": "/volumes/canonical"}],
    )
    assert container_resource_mounts_require_workspace_storage(
        workspace=control.workspaces.get_workspace(unprovisioned.id),
        mounts=volume_only_mounts,
    )


@pytest.mark.parametrize(
    ("workspace_name", "volume_id"),
    [
        ("../workspace", "volume-1"),
        ("workspace-1", "../volume"),
        ("workspace/other", "volume-1"),
        ("workspace-1", "volume\\other"),
    ],
)
def test_platform_volume_local_path_rejects_namespace_traversal(
    workspace_name: str,
    volume_id: str,
) -> None:
    with pytest.raises(ValueError, match=r"unsafe (workspace_name|volume_id)"):
        platform_volume_local_path(workspace_name=workspace_name, volume_id=volume_id)
