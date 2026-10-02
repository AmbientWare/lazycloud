from __future__ import annotations

from lazycloud.clients.api import ApiClient
from lazycloud.clients.storage import StorageClient
from lazycloud.clients.workloads import WorkloadsClient
from lazycloud.control import (
    ControlClientConfig,
    api_client,
    require_workspace,
    resolve_control_client_config,
    storage_client,
    workloads_client,
)


def control_config(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> ControlClientConfig:
    return resolve_control_client_config(workspace=workspace, timeout_seconds=timeout_seconds)


def api_session(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[ApiClient, str]:
    """The public API client and the workspace a command acts in."""
    config = control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    return api_client(config), require_workspace(config)


def workloads(*, workspace: str | None = None, timeout_seconds: float = 10.0) -> WorkloadsClient:
    """The workload operations of the workspace a command acts in."""
    return workloads_client(control_config(workspace=workspace, timeout_seconds=timeout_seconds))


def workspace_storage(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> StorageClient:
    return storage_client(control_config(workspace=workspace, timeout_seconds=timeout_seconds))


__all__ = [
    "api_session",
    "control_config",
    "workloads",
    "workspace_storage",
]
