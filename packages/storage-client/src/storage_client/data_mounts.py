from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shared.contracts import ContractModel

from storage_client.mounts import (
    JuiceFsMountConfig,
    JuiceFsMountManager,
    MountPointConfig,
    MountPointMountManager,
    StorageMountManager,
    StorageMountMode,
    StorageMountResult,
    StorageMountStatus,
    StorageMountSystem,
)

DEFAULT_DATA_STORAGE_PATH = "/data"


class MountedDataStorageError(RuntimeError):
    pass


class MountedDataStorageConfig(ContractModel):
    mode: StorageMountMode = StorageMountMode.Local
    local_path: str = DEFAULT_DATA_STORAGE_PATH
    format_juicefs: bool = True
    juicefs: JuiceFsMountConfig | None = None
    mountpoint: MountPointConfig | None = None


@dataclass(slots=True)
class MountedDataStorageManager:
    config: MountedDataStorageConfig
    system: StorageMountSystem = field(default_factory=StorageMountSystem)

    def ensure_mounted(self) -> StorageMountResult:
        local_path = self.config.local_path
        if self.config.mode is StorageMountMode.Local:
            Path(local_path).mkdir(parents=True, exist_ok=True)
            return StorageMountResult(
                mode=StorageMountMode.Local,
                local_path=local_path,
                status=StorageMountStatus.AlreadyMounted,
                reason="local data storage path ready",
            )

        manager = self._mount_manager()
        if isinstance(manager, JuiceFsMountManager) and self.config.format_juicefs:
            formatted = manager.format()
            if not formatted.ok:
                raise MountedDataStorageError(
                    formatted.output or formatted.reason or "juicefs format failed"
                )
        mounted = manager.mount(local_path)
        if not mounted.ok:
            raise MountedDataStorageError(
                mounted.output or mounted.reason or "data storage mount failed"
            )
        return mounted

    def _mount_manager(self) -> StorageMountManager:
        match self.config.mode:
            case StorageMountMode.JuiceFs:
                if self.config.juicefs is None:
                    msg = "juicefs data storage settings are required"
                    raise MountedDataStorageError(msg)
                return JuiceFsMountManager(self.config.juicefs, system=self.system)
            case StorageMountMode.MountPoint:
                if self.config.mountpoint is None:
                    msg = "mountpoint data storage settings are required"
                    raise MountedDataStorageError(msg)
                return MountPointMountManager(self.config.mountpoint, system=self.system)
            case StorageMountMode.Local:
                msg = "local data storage does not need a mount manager"
                raise MountedDataStorageError(msg)


def s3_bucket_url(*, endpoint_url: str, bucket_name: str, force_path_style: bool) -> str:
    if not bucket_name:
        msg = "bucket_name is required"
        raise ValueError(msg)
    endpoint = endpoint_url.rstrip("/")
    if endpoint and force_path_style:
        return f"{endpoint}/{bucket_name}"
    if endpoint:
        return f"s3://{bucket_name}"
    return bucket_name


__all__ = [
    "DEFAULT_DATA_STORAGE_PATH",
    "MountedDataStorageConfig",
    "MountedDataStorageError",
    "MountedDataStorageManager",
    "s3_bucket_url",
]
