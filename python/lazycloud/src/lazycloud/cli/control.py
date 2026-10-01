from __future__ import annotations

from lazycloud.clients.compute.control import ComputeClient
from lazycloud.clients.domain.control import DomainControlClient
from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.clients.ssh.control import SshControlClient
from lazycloud.clients.storage import StorageClient
from lazycloud.control import ControlClientConfig, resolve_control_client_config, storage_client
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


def domain_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> DomainControlClient:
    config = control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    return DomainControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def workspace_storage(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> StorageClient:
    return storage_client(control_config(workspace=workspace, timeout_seconds=timeout_seconds))


__all__ = [
    "compute_client",
    "control_config",
    "gateway_client",
    "ssh_client",
    "workspace_storage",
]
