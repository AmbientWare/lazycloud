from __future__ import annotations

from lazycloud.clients.compute.control import ComputeClient
from lazycloud.clients.domain.control import DomainControlClient
from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.clients.observability.control import ObservabilityControlClient
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.clients.secret.control import SecretControlClient
from lazycloud.clients.volume.control import VolumeControlClient
from lazycloud.clients.workspace.control import WorkspaceControlClient
from lazycloud.control import ControlClientConfig, resolve_control_client_config
from lazycloud.control_clients import (
    gateway_control_client,
    observability_control_client,
    resource_control_client,
)
from lazycloud.session.task import TaskClient


def control_config(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> ControlClientConfig:
    return resolve_control_client_config(workspace=workspace, timeout_seconds=timeout_seconds)


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


def observability_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> ObservabilityControlClient:
    return observability_control_client(
        control_config(workspace=workspace, timeout_seconds=timeout_seconds)
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


def secret_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> SecretControlClient:
    config = control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    return SecretControlClient.from_endpoint(
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


def workspace_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> WorkspaceControlClient:
    config = control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    return WorkspaceControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def task_client(
    *,
    workspace: str | None = None,
    timeout_seconds: float = 10.0,
) -> TaskClient:
    config = control_config(workspace=workspace, timeout_seconds=timeout_seconds)
    return TaskClient(
        workspace=config.workspace,
        endpoint=config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    )


__all__ = [
    "compute_client",
    "control_config",
    "gateway_client",
    "observability_client",
    "secret_client",
    "task_client",
    "volume_client",
    "workspace_client",
]
