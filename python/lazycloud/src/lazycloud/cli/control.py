from __future__ import annotations

from lazycloud.clients.api import ApiClient
from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.clients.ssh.control import SshControlClient
from lazycloud.clients.storage import StorageClient
from lazycloud.control import (
    ControlClientConfig,
    api_client,
    require_workspace,
    resolve_control_client_config,
    storage_client,
)
from lazycloud.control_clients import (
    gateway_control_client,
    resource_control_client,
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


def ssh_client(*, workspace: str | None = None) -> SshControlClient:
    config = control_config(workspace=workspace)
    return SshControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def gateway_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> GatewayControlClient:
    return gateway_control_client(
        control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    )


def resource_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> ResourceControlClient:
    return resource_control_client(
        control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    )


def workspace_storage(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> StorageClient:
    return storage_client(control_config(workspace=workspace, timeout_seconds=timeout_seconds))


__all__ = [
    "api_session",
    "control_config",
    "gateway_client",
    "ssh_client",
    "workspace_storage",
]
