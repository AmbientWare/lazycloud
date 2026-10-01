from __future__ import annotations

from lazycloud.clients.api import ApiClient
from lazycloud.clients.compute.control import ComputeClient
from lazycloud.clients.disk.control import DiskControlClient
from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.clients.ssh.control import SshControlClient
from lazycloud.clients.volume.control import VolumeControlClient
from lazycloud.control import (
    ControlClientConfig,
    api_client,
    require_workspace,
    resolve_control_client_config,
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


def compute_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> ComputeClient:
    config = control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    return ComputeClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def disk_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> DiskControlClient:
    config = control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    return DiskControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def volume_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> VolumeControlClient:
    config = control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    return VolumeControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


__all__ = [
    "api_session",
    "compute_client",
    "control_config",
    "gateway_client",
    "ssh_client",
    "volume_client",
]
