from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from shared.container_requests import RequestMount, RequestMountType
from storage_client.mounts import (
    MountPointConfig,
    MountPointMountManager,
    StorageMountManager,
    StorageMountStatus,
    StorageMountSystem,
)

from worker.container_startup import (
    WorkerMountPointBackend,
    WorkerMountPointRequest,
    WorkerMountPointResult,
    WorkerMountPointStatus,
)

DEFAULT_REQUEST_MOUNT_ROOT = "/tmp/external-volumes"


class WorkerRequestMountCleaner(Protocol):
    def unmount_request_mounts(self, container_id: str) -> None: ...

    def unmount_all(self) -> None: ...


class WorkerRequestMountLifecycle(WorkerMountPointBackend, WorkerRequestMountCleaner, Protocol):
    pass


class WorkerRequestMountError(RuntimeError):
    pass


@dataclass(slots=True)
class _RequestMountRecord:
    mount: RequestMount
    manager: StorageMountManager


@dataclass(slots=True)
class WorkerRequestMountManager:
    mount_root: Path = Path(DEFAULT_REQUEST_MOUNT_ROOT)
    mountpoint_binary: str = "ms3"
    system: StorageMountSystem = field(default_factory=StorageMountSystem)
    _mounts: dict[str, dict[Path, _RequestMountRecord]] = field(default_factory=dict)
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock)

    def mount_request_mount(self, request: WorkerMountPointRequest) -> WorkerMountPointResult:
        mount = request.mount
        config = mount.mountpoint_config
        if mount.mount_type is not RequestMountType.MountPoint or config is None:
            return self._failed(mount, "request is not an S3 Mountpoint mount")
        try:
            local_path = self._owned_local_path(mount.local_path)
        except WorkerRequestMountError as exc:
            return self._failed(mount, str(exc))

        with self._lock(request.container_id):
            records = self._mounts.setdefault(request.container_id, {})
            existing = records.get(local_path)
            if existing is not None:
                if not _same_mount(existing.mount, mount):
                    return self._failed(
                        mount,
                        f"conflicting S3 mount configuration for {local_path}",
                    )
                if self.system.mount_checker(str(local_path)):
                    return WorkerMountPointResult(
                        status=WorkerMountPointStatus.Mounted,
                        local_path=str(local_path),
                        mount_path=mount.mount_path,
                        reason="S3 mount already prepared for container",
                    )
                self._unmount_record(existing, local_path)
                records.pop(local_path, None)

            manager = MountPointMountManager(
                MountPointConfig(
                    bucket_name=config.bucket_name,
                    prefix=config.prefix,
                    access_key=config.access_key,
                    secret_key=config.secret_key,
                    endpoint_url=config.endpoint_url,
                    region=config.region,
                    read_only=mount.read_only,
                    force_path_style=config.force_path_style,
                    binary=self.mountpoint_binary,
                ),
                system=self.system,
            )
            mounted = manager.mount(str(local_path))
            if not mounted.ok:
                return self._failed(
                    mount,
                    mounted.reason or mounted.output or "S3 mount failed",
                    local_path=local_path,
                )
            records[local_path] = _RequestMountRecord(mount=mount, manager=manager)
            return WorkerMountPointResult(
                status=WorkerMountPointStatus.Mounted,
                local_path=str(local_path),
                mount_path=mount.mount_path,
                reason=mounted.reason or "S3 bucket mounted",
            )

    def unmount_request_mounts(self, container_id: str) -> None:
        with self._lock(container_id):
            records = self._mounts.get(container_id, {})
            failures: list[str] = []
            for local_path, record in list(records.items()):
                result = record.manager.unmount(str(local_path))
                if result.status is StorageMountStatus.Failed:
                    failures.append(str(local_path))
                    continue
                records.pop(local_path, None)
                self._prune_empty_parents(local_path.parent)
            if not records:
                self._mounts.pop(container_id, None)
            if failures:
                joined = ", ".join(sorted(failures))
                raise WorkerRequestMountError(f"failed to unmount S3 mounts: {joined}")

    def unmount_all(self) -> None:
        failures: list[str] = []
        for container_id in list(self._mounts):
            try:
                self.unmount_request_mounts(container_id)
            except WorkerRequestMountError:
                failures.append(container_id)
        if failures:
            joined = ", ".join(sorted(failures))
            raise WorkerRequestMountError(f"failed to unmount S3 mounts for containers: {joined}")

    def _unmount_record(self, record: _RequestMountRecord, local_path: Path) -> None:
        result = record.manager.unmount(str(local_path))
        if result.status is StorageMountStatus.Failed:
            raise WorkerRequestMountError(
                result.reason or result.output or f"failed to unmount {local_path}"
            )

    def _owned_local_path(self, value: str) -> Path:
        if not value:
            raise WorkerRequestMountError("S3 mount local path is required")
        root = self.mount_root.resolve()
        path = Path(value).resolve()
        if not path.is_relative_to(root):
            raise WorkerRequestMountError(f"S3 mount path is outside {root}")
        return path

    def _lock(self, container_id: str) -> threading.Lock:
        if not container_id:
            raise WorkerRequestMountError("container id is required for S3 mounts")
        with self._locks_guard:
            lock = self._locks.get(container_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[container_id] = lock
            return lock

    def _prune_empty_parents(self, start: Path) -> None:
        root = self.mount_root.resolve()
        current = start.resolve()
        while current != root and current.is_relative_to(root):
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def _failed(
        self,
        mount: RequestMount,
        reason: str,
        *,
        local_path: Path | None = None,
    ) -> WorkerMountPointResult:
        return WorkerMountPointResult(
            status=WorkerMountPointStatus.Failed,
            local_path=str(local_path) if local_path is not None else mount.local_path,
            mount_path=mount.mount_path,
            reason=reason,
        )


def _same_mount(first: RequestMount, second: RequestMount) -> bool:
    return (
        first.local_path == second.local_path
        and first.read_only == second.read_only
        and first.mountpoint_config == second.mountpoint_config
    )


__all__ = [
    "DEFAULT_REQUEST_MOUNT_ROOT",
    "WorkerRequestMountCleaner",
    "WorkerRequestMountError",
    "WorkerRequestMountLifecycle",
    "WorkerRequestMountManager",
]
