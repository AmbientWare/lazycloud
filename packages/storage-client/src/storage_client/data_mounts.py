from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import field_validator
from shared.contracts import ContractModel

from storage_client.mounts import (
    JuiceFsMountConfig,
    JuiceFsMountManager,
    MountPointConfig,
    MountPointMountManager,
    StorageMountManager,
    StorageMountMode,
    StorageMountResult,
    StorageMountSystem,
)

DEFAULT_DATA_STORAGE_PATH = "/data"


class MountedDataStorageError(RuntimeError):
    pass


class MountedDataStorageConfig(ContractModel):
    """Durable data storage for a worker.

    There is no local mode. Data storage holds volumes and outputs, which must
    outlive the container that wrote them, and a local directory silently
    accepts writes and loses them. Backing it always by an object store or a
    shared filesystem makes that failure unrepresentable rather than guarded.
    """

    mode: StorageMountMode = StorageMountMode.JuiceFs
    local_path: str = DEFAULT_DATA_STORAGE_PATH
    format_juicefs: bool = True
    juicefs: JuiceFsMountConfig | None = None
    mountpoint: MountPointConfig | None = None

    @field_validator("mode")
    @classmethod
    def mode_must_be_durable(cls, value: StorageMountMode) -> StorageMountMode:
        if value is StorageMountMode.Local:
            msg = "data storage must be juicefs or mountpoint; local storage is not durable"
            raise ValueError(msg)
        return value


@dataclass(slots=True)
class MountedDataStorageManager:
    config: MountedDataStorageConfig
    system: StorageMountSystem = field(default_factory=StorageMountSystem)

    def ensure_mounted(self) -> StorageMountResult:
        local_path = self.config.local_path
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
                msg = "data storage must be juicefs or mountpoint; local storage is not durable"
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
