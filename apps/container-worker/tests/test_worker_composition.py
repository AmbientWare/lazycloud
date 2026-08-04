from __future__ import annotations

from pathlib import Path

import pytest
from container_worker_app.composition import build_worker_process_services
from container_worker_app.settings import WorkerSettings
from worker.configuration import WorkerConfiguration, WorkerPathConfiguration
from worker.repository_client import WorkerRepositoryClientError
from worker.runtime_config import OciRuntimeName, RuntimeBinaryConfig

_CAPACITY_OWNER_ID = "839fc92e-c26d-4e31-84c1-a827c2768607"


@pytest.mark.parametrize(
    ("repository_url", "worker_token", "message"),
    [
        ("", "worker-token", "worker repository endpoint is required"),
        ("http://control-plane:9000", "", "worker repository token is required"),
    ],
)
def test_worker_requires_repository_credentials_before_registration(
    tmp_path: Path,
    repository_url: str,
    worker_token: str,
    message: str,
) -> None:
    with pytest.raises(WorkerRepositoryClientError, match=message):
        build_worker_process_services(
            settings=WorkerSettings(
                worker_id="worker-1",
                worker_repository_url=repository_url,
                worker_token=worker_token,
                capacity_owner_id=_CAPACITY_OWNER_ID,
                configuration=WorkerConfiguration(
                    paths=WorkerPathConfiguration(
                        bundle_root=tmp_path / "bundles",
                        image_cache_path=str(tmp_path / "image-cache"),
                        image_mount_root=str(tmp_path / "image-mounts"),
                        checkpoint_root=str(tmp_path / "checkpoints"),
                        image_build_root=tmp_path / "builds",
                    )
                ),
            ),
            runtime_configs={OciRuntimeName.Runc: RuntimeBinaryConfig()},
        )
