from __future__ import annotations

from pathlib import Path

from foundation.process import ProcessResult
from storage_client.mounts import StorageMountSystem
from tests.fakes import FakeManagedCommand
from worker.cache_assets import WorkspaceGeeseFsStorageConfig, WorkspaceStorageConfig
from worker.events import ContainerRequestContext
from worker.tools import WorkspaceStorageCredentials
from worker.workspace_storage import WorkerWorkspaceStorageManager


def test_cleanup_keeps_a_workspace_mount_another_container_ensured(tmp_path: Path) -> None:
    mounted: set[str] = set()
    manager = WorkerWorkspaceStorageManager(
        config=WorkspaceStorageConfig(
            base_mount_path=str(tmp_path / "workspace"),
            geesefs=WorkspaceGeeseFsStorageConfig(cache_root=str(tmp_path / "cache")),
        ),
        system=StorageMountSystem(
            mount_checker=lambda path: path in mounted,
            start_command=lambda argv, _env: mounted.add(argv[-1]) or FakeManagedCommand(argv),
            run_command=lambda _timeout, argv: (
                mounted.discard(argv[-1])
                or ProcessResult(args=argv, exit_code=0, stdout="", stderr="")
            ),
        ),
        credential_root=str(tmp_path / "credentials"),
    )
    mount_path = str(tmp_path / "workspace" / "tenant")

    manager.ensure_workspace_storage(_request("ctr-stopping"))
    # Still starting: ensured its storage but not yet registered as an instance.
    manager.ensure_workspace_storage(_request("ctr-starting"))

    assert manager.release_workspace_storage("ctr-stopping") == []
    assert mount_path in mounted

    [released] = manager.release_workspace_storage("ctr-starting")
    assert released.ok
    assert mount_path not in mounted


def _request(container_id: str) -> ContainerRequestContext:
    return ContainerRequestContext(
        container_id=container_id,
        workspace_name="tenant",
        workspace_storage_available=True,
        workspace_storage_required=True,
        workspace_storage_credentials=WorkspaceStorageCredentials(
            endpoint_url="http://127.0.0.1:9000",
            region="us-east-1",
            bucket_name="bucket",
            access_key="key",
            secret_key="secret",
        ),
    )
