from __future__ import annotations

import posixpath
from enum import StrEnum

from pydantic import Field
from shared.contracts import ContractModel


class WorkspaceStorageMountAction(StrEnum):
    Reuse = "reuse"
    Mount = "mount"
    Remount = "remount"
    Reject = "reject"


class WorkspaceStorageCredentials(ContractModel):
    endpoint_url: str | None = None
    bucket_name: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    region: str | None = None

    @property
    def complete(self) -> bool:
        return all(
            (
                self.endpoint_url,
                self.bucket_name,
                self.access_key,
                self.secret_key,
                self.region,
            )
        )


class WorkspaceGeeseFsStorageConfig(ContractModel):
    binary: str = "geesefs"
    cache_root: str = "/var/lib/lazycloud/geesefs-cache"
    memory_limit_mb: int = 1024
    max_flushers: int = 16
    stat_cache_ttl_seconds: int = 1


class WorkspaceStorageConfig(ContractModel):
    base_mount_path: str = "/workspace"
    geesefs: WorkspaceGeeseFsStorageConfig = Field(
        default_factory=WorkspaceGeeseFsStorageConfig
    )


class WorkspaceMountState(ContractModel):
    workspace_name: str
    mount_path: str
    mounted: bool = True


class WorkspaceStorageMountPlan(ContractModel):
    workspace_name: str
    mount_path: str
    action: WorkspaceStorageMountAction
    valid: bool
    reason: str = ""
    requires_credentials: bool = True
    lock_key: str = ""
    unmount_existing: bool = False
    remove_mount_path: bool = False
    previous_mount_path: str = ""


class WorkspaceStorageCleanupPlan(ContractModel):
    active_workspaces: set[str] = Field(default_factory=set)
    unmount: list[WorkspaceMountState] = Field(default_factory=list)
    keep: list[WorkspaceMountState] = Field(default_factory=list)


def validate_workspace_storage(
    credentials: WorkspaceStorageCredentials | None,
) -> tuple[bool, str]:
    if credentials is None:
        return (False, "workspace storage metadata is required")
    if not credentials.complete:
        return (False, "workspace storage metadata is incomplete")
    return (True, "workspace storage metadata complete")


def workspace_storage_mount_lock_key(workspace_name: str) -> str:
    if not workspace_name:
        msg = "workspace_name is required"
        raise ValueError(msg)
    return f"workspace-storage:{workspace_name}"


def plan_workspace_storage_mount(
    workspace_name: str,
    *,
    credentials: WorkspaceStorageCredentials | None,
    config: WorkspaceStorageConfig = WorkspaceStorageConfig(),
    existing: WorkspaceMountState | None = None,
) -> WorkspaceStorageMountPlan:
    mount_path = posixpath.join(config.base_mount_path.rstrip("/"), workspace_name)
    lock_key = workspace_storage_mount_lock_key(workspace_name)
    if existing is not None and workspace_mount_healthy(existing):
        return WorkspaceStorageMountPlan(
            workspace_name=workspace_name,
            mount_path=existing.mount_path,
            action=WorkspaceStorageMountAction.Reuse,
            valid=True,
            reason="existing mount is healthy",
            requires_credentials=False,
            lock_key=lock_key,
        )
    stale = existing is not None
    valid, reason = validate_workspace_storage(credentials)
    action = (
        WorkspaceStorageMountAction.Remount
        if valid and stale
        else WorkspaceStorageMountAction.Mount
    )
    return WorkspaceStorageMountPlan(
        workspace_name=workspace_name,
        mount_path=mount_path,
        action=action if valid else WorkspaceStorageMountAction.Reject,
        valid=valid,
        reason=reason,
        lock_key=lock_key,
        unmount_existing=stale,
        remove_mount_path=stale,
        previous_mount_path=existing.mount_path if existing is not None else "",
    )


def workspace_mount_healthy(mount: WorkspaceMountState) -> bool:
    return mount.mounted


def plan_workspace_mount_cleanup(
    mounts: list[WorkspaceMountState],
    *,
    active_workspace_names: set[str],
) -> WorkspaceStorageCleanupPlan:
    keep: list[WorkspaceMountState] = []
    unmount: list[WorkspaceMountState] = []
    for mount in mounts:
        if mount.workspace_name in active_workspace_names:
            keep.append(mount)
        else:
            unmount.append(mount)
    return WorkspaceStorageCleanupPlan(
        active_workspaces=active_workspace_names,
        keep=keep,
        unmount=unmount,
    )
