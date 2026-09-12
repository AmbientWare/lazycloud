from __future__ import annotations

from ipaddress import IPv4Network
from itertools import pairwise

from pydantic import Field, model_validator

from shared.http.base import HttpModel


class WireGuardGatewayConfiguration(HttpModel):
    index: int = Field(ge=0, le=31)
    public_key: str = Field(min_length=44, max_length=44)
    endpoint: str = Field(min_length=3, max_length=512)


class WireGuardRouteConfiguration(HttpModel):
    network: str
    gateway_indices: tuple[int, ...]

    @model_validator(mode="after")
    def validate_route(self) -> WireGuardRouteConfiguration:
        IPv4Network(self.network)
        if len(set(self.gateway_indices)) != len(self.gateway_indices):
            raise ValueError("WireGuard route gateway indices must be unique")
        return self


class WireGuardPeerConfiguration(HttpModel):
    peer_id: str = Field(min_length=1)
    address: str
    allowed_ips: tuple[str, ...]
    persistent_keepalive_seconds: int = Field(default=25, ge=1, le=120)
    generation: int = Field(ge=1)
    gateways: tuple[WireGuardGatewayConfiguration, ...] = Field(min_length=1)
    routes: tuple[WireGuardRouteConfiguration, ...]

    @model_validator(mode="after")
    def validate_gateways(self) -> WireGuardPeerConfiguration:
        if len({gateway.index for gateway in self.gateways}) != len(self.gateways):
            raise ValueError("WireGuard gateway indices must be unique")
        if len({gateway.public_key for gateway in self.gateways}) != len(self.gateways):
            raise ValueError("WireGuard gateway public keys must be unique")
        indices = {gateway.index for gateway in self.gateways}
        allowed = tuple(IPv4Network(network) for network in self.allowed_ips)
        networks: list[IPv4Network] = []
        for route in self.routes:
            network = IPv4Network(route.network)
            if not any(network.subnet_of(parent) for parent in allowed):
                raise ValueError("WireGuard route must be within the peer's allowed networks")
            if not set(route.gateway_indices).issubset(indices):
                raise ValueError("WireGuard route refers to an unconfigured gateway")
            networks.append(network)
        ordered = sorted(networks, key=lambda network: int(network.network_address))
        if any(left.overlaps(right) for left, right in pairwise(ordered)):
            raise ValueError("WireGuard destination routes must not overlap")
        return self


class RegisterPrivateNetworkRequest(HttpModel):
    agent_token: str
    public_key: str = Field(min_length=44, max_length=44)


class PrivateNetworkTopologyRequest(HttpModel):
    agent_token: str


__all__ = [
    "PrivateNetworkTopologyRequest",
    "RegisterPrivateNetworkRequest",
    "WireGuardGatewayConfiguration",
    "WireGuardPeerConfiguration",
    "WireGuardRouteConfiguration",
]
