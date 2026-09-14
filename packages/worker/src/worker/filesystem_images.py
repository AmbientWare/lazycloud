from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from shared.app_identity import CONTAINER_HELPER_PATH
from shared.container_requests import RuntimeContainerStatus
from shared.env import parse_environment

from worker.container_service.protocols import (
    WorkerContainerInstanceStore,
    WorkerContainerRuntimeController,
)
from worker.sandbox_server import (
    WORKER_CONTAINER_UPLOADS_HOST_PATH,
    WORKER_CONTAINER_UPLOADS_MOUNT_PATH,
)


@dataclass(slots=True)
class ContainerFilesystemExporter:
    instances: WorkerContainerInstanceStore
    runtime: WorkerContainerRuntimeController

    def export(self, container_id: str, *, workspace_id: str, context_dir: Path) -> list[str]:
        instance = self.instances.get_container_instance(container_id)
        if instance is None or instance.workspace_id != workspace_id:
            raise RuntimeError("filesystem source container is not assigned to this worker")
        if self.runtime.status(container_id) != RuntimeContainerStatus.Running.value:
            raise RuntimeError("filesystem source container is no longer running")
        filename = f"snapshot-{uuid4().hex}.tar"
        staged = Path(WORKER_CONTAINER_UPLOADS_HOST_PATH) / container_id / filename
        try:
            result = self.runtime.exec_container(
                container_id,
                argv=[
                    CONTAINER_HELPER_PATH,
                    "snapshot-filesystem",
                    f"{WORKER_CONTAINER_UPLOADS_MOUNT_PATH}/{filename}",
                ],
                env=[],
                cwd="/",
            )
            if not result.ok:
                raise RuntimeError(result.error_msg or "filesystem capture failed")
            descriptor = os.open(staged, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise RuntimeError("filesystem capture did not produce a regular archive")
                with (context_dir / "rootfs.tar").open("wb") as target:
                    shutil.copyfileobj(source, target)
        finally:
            staged.unlink(missing_ok=True)
        return [f"{name}={value}" for name, value in parse_environment(instance.image_env).items()]
