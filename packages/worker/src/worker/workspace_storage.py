from __future__ import annotations

import posixpath
import shutil
import threading
from dataclasses import dataclass, field
from enum import StrEnum

from shared.container_requests import (
    DEFAULT_ARTIFACTS_PATH,
    WORKER_USER_ARTIFACT_VOLUME,
    RequestMount,
    RequestMountType,
)
from shared.contracts import ContractModel
from storage_client.mounts import (
    GeeseFsMountConfig,
    GeeseFsMountManager,
    StorageMountManager,
    StorageMountResult,
    StorageMountStatus,
    StorageMountSystem,
    geesefs_memory_limit_mb,
)

from worker import cache_assets
from worker.cache_assets import (
    WorkspaceMountState,
    WorkspaceStorageConfig,
    WorkspaceStorageMountAction,
    plan_workspace_mount_cleanup,
    plan_workspace_storage_mount,
)
from worker.events import ContainerRequestContext
from worker.tools import WorkspaceStorageCredentials


class WorkspaceStorageEnsureStatus(StrEnum):
    Skipped = "skipped"
    Reused = "reused"
    Mounted = "mounted"
    Remounted = "remounted"


class WorkspaceStorageEnsureResult(ContractModel):
    workspace_name: str
    mount_path: str = ""
    status: WorkspaceStorageEnsureStatus
    reason: str = ""
    mount: StorageMountResult | None = None

    @property
    def mounted(self) -> bool:
        return self.status in {
            WorkspaceStorageEnsureStatus.Reused,
            WorkspaceStorageEnsureStatus.Mounted,
            WorkspaceStorageEnsureStatus.Remounted,
        }


@dataclass(slots=True)
class _WorkspaceMountRecord:
    workspace_name: str
    mount_path: str
    manager: StorageMountManager


class WorkerWorkspaceStorageError(RuntimeError):
    pass


@dataclass(slots=True)
class WorkerWorkspaceStorageManager:
    config: WorkspaceStorageConfig = field(default_factory=WorkspaceStorageConfig)
    system: StorageMountSystem = field(default_factory=StorageMountSystem)
    _mounts: dict[str, _WorkspaceMountRecord] = field(default_factory=dict)
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _locks_lock: threading.Lock = field(default_factory=threading.Lock)

    def ensure_workspace_storage(
        self,
        request: ContainerRequestContext,
    ) -> WorkspaceStorageEnsureResult:
        workspace_name = request.workspace_name or request.workspace_id
        required, reason = request_requires_workspace_storage_mount(request)
        if not required:
            return WorkspaceStorageEnsureResult(
                workspace_name=workspace_name,
                status=WorkspaceStorageEnsureStatus.Skipped,
                reason=reason,
            )
        if not workspace_name:
            msg = "workspace name is required for workspace storage mount"
            raise WorkerWorkspaceStorageError(msg)

        with self._lock(workspace_name):
            existing = self._mount_state(workspace_name)
            credentials = _planned_credentials(request.workspace_storage_credentials)
            plan = plan_workspace_storage_mount(
                workspace_name,
                credentials=credentials,
                config=self.config,
                existing=existing,
            )
            if plan.action is WorkspaceStorageMountAction.Reuse:
                return WorkspaceStorageEnsureResult(
                    workspace_name=workspace_name,
                    mount_path=plan.mount_path,
                    status=WorkspaceStorageEnsureStatus.Reused,
                    reason=plan.reason,
                )
            if not plan.valid:
                raise WorkerWorkspaceStorageError(plan.reason or "workspace storage mount rejected")
            if plan.unmount_existing:
                self._unmount_existing(workspace_name)
            manager = self._mount_manager(workspace_name, request.workspace_storage_credentials)
            mounted = self._mount(manager, plan.mount_path)
            self._mounts[workspace_name] = _WorkspaceMountRecord(
                workspace_name=workspace_name,
                mount_path=plan.mount_path,
                manager=manager,
            )
            return WorkspaceStorageEnsureResult(
                workspace_name=workspace_name,
                mount_path=plan.mount_path,
                status=(
                    WorkspaceStorageEnsureStatus.Remounted
                    if plan.action is WorkspaceStorageMountAction.Remount
                    else WorkspaceStorageEnsureStatus.Mounted
                ),
                reason=mounted.reason,
                mount=mounted,
            )

    def cleanup_unused(
        self,
        *,
        active_workspace_names: set[str],
    ) -> list[StorageMountResult]:
        plan = plan_workspace_mount_cleanup(
            [self._state_from_record(record) for record in self._mounts.values()],
            active_workspace_names=active_workspace_names,
        )
        results: list[StorageMountResult] = []
        for state in plan.unmount:
            with self._lock(state.workspace_name):
                record = self._mounts.get(state.workspace_name)
                if record is None:
                    continue
                result = record.manager.unmount(record.mount_path)
                results.append(result)
                if not result.ok:
                    continue
                self._mounts.pop(state.workspace_name, None)
                shutil.rmtree(record.mount_path, ignore_errors=True)
                self._remove_cache_dir(state.workspace_name)
        return results

    def _mount_state(self, workspace_name: str) -> WorkspaceMountState | None:
        record = self._mounts.get(workspace_name)
        if record is None:
            return None
        return self._state_from_record(record)

    def _state_from_record(self, record: _WorkspaceMountRecord) -> WorkspaceMountState:
        return WorkspaceMountState(
            workspace_name=record.workspace_name,
            mount_path=record.mount_path,
            mounted=self.system.mount_checker(record.mount_path),
        )

    def _mount(self, manager: StorageMountManager, mount_path: str) -> StorageMountResult:
        mounted = manager.mount(mount_path)
        if not mounted.ok:
            raise WorkerWorkspaceStorageError(
                mounted.output or mounted.reason or "workspace storage mount failed"
            )
        return mounted

    def _mount_manager(
        self,
        workspace_name: str,
        credentials: WorkspaceStorageCredentials | None,
    ) -> StorageMountManager:
        complete = _require_credentials(credentials)
        geesefs = self.config.geesefs
        return GeeseFsMountManager(
            GeeseFsMountConfig(
                bucket_name=complete.bucket_name,
                prefix=complete.prefix,
                endpoint_url=complete.endpoint_url,
                region=complete.region,
                access_key=complete.access_key,
                secret_key=complete.secret_key,
                force_path_style=complete.force_path_style,
                cache_dir=posixpath.join(geesefs.cache_root, workspace_name),
                memory_limit_mb=geesefs_memory_limit_mb(
                    configured_mb=geesefs.memory_limit_mb,
                    worker_memory_mib=geesefs.worker_memory_mib,
                ),
                max_flushers=geesefs.max_flushers,
                stat_cache_ttl_seconds=geesefs.stat_cache_ttl_seconds,
                binary=geesefs.binary,
            ),
            system=self.system,
        )

    def _remove_cache_dir(self, workspace_name: str) -> None:
        shutil.rmtree(
            posixpath.join(self.config.geesefs.cache_root, workspace_name),
            ignore_errors=True,
        )

    def _unmount_existing(self, workspace_name: str) -> None:
        record = self._mounts.pop(workspace_name, None)
        if record is None:
            return
        result = record.manager.unmount(record.mount_path)
        if result.status is StorageMountStatus.Failed:
            raise WorkerWorkspaceStorageError(
                result.output or result.reason or "workspace storage unmount failed"
            )
        shutil.rmtree(record.mount_path, ignore_errors=True)
        self._remove_cache_dir(workspace_name)

    def _lock(self, workspace_name: str) -> threading.Lock:
        with self._locks_lock:
            lock = self._locks.get(workspace_name)
            if lock is None:
                lock = threading.Lock()
                self._locks[workspace_name] = lock
            return lock


def request_requires_workspace_storage_mount(
    request: ContainerRequestContext,
) -> tuple[bool, str]:
    if not request.workspace_storage_available:
        return (False, "workspace storage unavailable")
    if request.workspace_storage_required:
        return (True, "request requires workspace storage")
    for mount in request.mounts:
        if _mount_requires_workspace_storage(mount):
            return (True, f"{mount.mount_path} requires workspace storage")
    return (False, "no workspace storage mount needed")


def _mount_requires_workspace_storage(mount: RequestMount) -> bool:
    if mount.mount_type is RequestMountType.Volume:
        return True
    if mount.mount_type is RequestMountType.MountPoint:
        return False
    mount_path = mount.mount_path.rstrip("/")
    local_path = mount.local_path.rstrip("/")
    return (
        mount_path == WORKER_USER_ARTIFACT_VOLUME
        or mount_path.startswith(WORKER_USER_ARTIFACT_VOLUME + "/")
        or local_path == DEFAULT_ARTIFACTS_PATH
        or local_path.startswith(DEFAULT_ARTIFACTS_PATH + "/")
    )


def _planned_credentials(
    credentials: WorkspaceStorageCredentials | None,
) -> cache_assets.WorkspaceStorageCredentials | None:
    if credentials is None:
        return None
    return cache_assets.WorkspaceStorageCredentials(
        endpoint_url=credentials.endpoint_url or None,
        bucket_name=credentials.bucket_name or None,
        access_key=credentials.access_key or None,
        secret_key=credentials.secret_key or None,
        region=credentials.region or None,
    )


def _require_credentials(
    credentials: WorkspaceStorageCredentials | None,
) -> WorkspaceStorageCredentials:
    if credentials is None:
        msg = "workspace storage metadata is required"
        raise WorkerWorkspaceStorageError(msg)
    missing = [
        name
        for name in ("endpoint_url", "bucket_name", "access_key", "secret_key", "region")
        if not getattr(credentials, name)
    ]
    if missing:
        msg = f"workspace storage metadata is incomplete: {', '.join(missing)}"
        raise WorkerWorkspaceStorageError(msg)
    return credentials


__all__ = [
    "WorkerWorkspaceStorageError",
    "WorkerWorkspaceStorageManager",
    "WorkspaceStorageEnsureResult",
    "WorkspaceStorageEnsureStatus",
    "request_requires_workspace_storage_mount",
]
