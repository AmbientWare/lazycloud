from __future__ import annotations

from pathlib import Path

from foundation.process import ProcessResult
from shared.container_requests import RequestMount, RequestMountPointConfig, RequestMountType
from shared.mounts import MountAuthMode
from storage_client.mounts import StorageMountSystem
from tests.fakes import FakeManagedCommand
from worker.container_startup import WorkerMountPointRequest, WorkerMountPointStatus
from worker.request_mounts import WorkerRequestMountManager


def test_request_mount_manager_rejects_paths_outside_owned_root(tmp_path: Path) -> None:
    starts: list[list[str]] = []
    manager = WorkerRequestMountManager(
        mount_root=tmp_path / "external",
        system=StorageMountSystem(
            mount_checker=lambda _path: False,
            start_command=lambda argv, _env: starts.append(argv) or FakeManagedCommand(argv),
            run_command=lambda _timeout, argv: ProcessResult(
                args=argv,
                exit_code=0,
                stdout="",
                stderr="",
            ),
        ),
    )

    result = manager.mount_request_mount(
        WorkerMountPointRequest(
            container_id="ctr-1",
            mount=_mount(tmp_path / "outside"),
        )
    )

    assert result.status is WorkerMountPointStatus.Failed
    assert "outside" in result.reason
    assert starts == []


def _mount(local_path: Path) -> RequestMount:
    return RequestMount(
        local_path=str(local_path),
        mount_path="/volumes/bucket",
        mount_type=RequestMountType.MountPoint,
        mountpoint_config=RequestMountPointConfig(
            bucket_name="bucket",
            prefix="models/",
            auth_mode=MountAuthMode.SecretReferences,
            access_key="access-value",
            secret_key="secret-value",
            endpoint_url="http://object-store:9000",
            region="us-east-1",
            force_path_style=True,
        ),
    )
