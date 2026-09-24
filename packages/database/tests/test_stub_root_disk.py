from __future__ import annotations

from uuid import uuid4

from database.records.apps import StubRecord
from database.repositories.apps import StubRepository
from database.repositories.identity import WorkspaceRepository
from shared.deployments import StubKind
from shared.disks import DiskMount
from shared.workload_config import StubAutoscalerConfig, StubConfig
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

GIB = 1024**3


def test_a_disk_stored_at_the_default_mount_path_is_the_root_disk(
    migrated_database_url: URL,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=migrated_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="root-disk")
            stubs = StubRepository(session)
            # The root disk's mount path is left unset, so the stored disk omits it.
            root, data = (
                stubs.upsert(
                    StubRecord(
                        id=str(uuid4()),
                        workspace_id=workspace.id,
                        name=disk.name,
                        kind=StubKind.Pod,
                        config=StubConfig(
                            disks=[disk], autoscaler=StubAutoscalerConfig(max_containers=1)
                        ),
                    )
                )
                for disk in (
                    DiskMount(name="box", size_bytes=10 * GIB),
                    DiskMount(name="data", size_bytes=GIB, mount_path="/data"),
                )
            )
            assert stubs.mounts_root_disk(root.id, workspace_id=workspace.id)
            assert not stubs.mounts_root_disk(data.id, workspace_id=workspace.id)
    finally:
        database.dispose()
