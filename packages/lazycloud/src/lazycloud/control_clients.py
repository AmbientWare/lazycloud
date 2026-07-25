from __future__ import annotations

from shared.http_transport import HttpChannel

from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.clients.observability.control import ObservabilityControlClient
from lazycloud.clients.pod.control import PodControlClient
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.control import ControlClientConfig


def control_http_channel(config: ControlClientConfig) -> HttpChannel:
    return HttpChannel(
        endpoint=config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    )


def gateway_control_client(config: ControlClientConfig) -> GatewayControlClient:
    return GatewayControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    )


def resource_control_client(config: ControlClientConfig) -> ResourceControlClient:
    return ResourceControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def observability_control_client(config: ControlClientConfig) -> ObservabilityControlClient:
    return ObservabilityControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def pod_control_client(config: ControlClientConfig) -> PodControlClient:
    return PodControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    )


__all__ = [
    "control_http_channel",
    "gateway_control_client",
    "observability_control_client",
    "pod_control_client",
    "resource_control_client",
]
