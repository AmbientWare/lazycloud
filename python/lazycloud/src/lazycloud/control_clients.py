from __future__ import annotations

from typing import TYPE_CHECKING

from lazycloud.control import ControlClientConfig

if TYPE_CHECKING:
    from lazycloud.clients.gateway.control import GatewayControlClient
    from lazycloud.clients.pod.control import PodControlClient
    from lazycloud.clients.resource.control import ResourceControlClient


def gateway_control_client(config: ControlClientConfig) -> GatewayControlClient:
    from lazycloud.clients.gateway.control import GatewayControlClient

    return GatewayControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        workspace=config.workspace,
        timeout_seconds=config.timeout_seconds,
    )


def resource_control_client(config: ControlClientConfig) -> ResourceControlClient:
    from lazycloud.clients.resource.control import ResourceControlClient

    return ResourceControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        workspace=config.workspace,
        timeout_seconds=config.timeout_seconds,
    )


def pod_control_client(config: ControlClientConfig) -> PodControlClient:
    from lazycloud.clients.pod.control import PodControlClient

    return PodControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        workspace=config.workspace,
        timeout_seconds=config.timeout_seconds,
    )


__all__ = [
    "gateway_control_client",
    "pod_control_client",
    "resource_control_client",
]
