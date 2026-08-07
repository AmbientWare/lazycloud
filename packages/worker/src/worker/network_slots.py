from __future__ import annotations

from enum import StrEnum

from foundation.validation import CidrValidation, validate_allow_list
from pydantic import Field
from shared.contracts import ContractModel

from worker.execution import (
    DEFAULT_CONTAINER_IPV6_SUBNET,
    DEFAULT_CONTAINER_SUBNET,
    container_ipv6_address,
    container_veth_names,
)
from worker.network_rules import container_network_comment


class NetworkRestrictionMode(StrEnum):
    Unrestricted = "unrestricted"
    Block = "block"
    Allowlist = "allowlist"


class ContainerNetworkInfo(ContractModel):
    container_id: str
    container_ip: str
    container_ipv6: str = ""
    namespace: str
    veth_host: str
    comment: str


class NetworkRestrictionPlan(ContractModel):
    mode: NetworkRestrictionMode
    apply: bool
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)
    cidrs: list[CidrValidation] = Field(default_factory=list)

    @property
    def ipv4_allow_list(self) -> list[str]:
        return [entry.normalized for entry in self.cidrs if not entry.is_ipv6]

    @property
    def ipv6_allow_list(self) -> list[str]:
        return [entry.normalized for entry in self.cidrs if entry.is_ipv6]


def container_network_info_from_ip(
    container_id: str,
    container_ip: str,
    *,
    ipv6_enabled: bool = False,
    ipv4_subnet: str = DEFAULT_CONTAINER_SUBNET,
    ipv6_subnet: str = DEFAULT_CONTAINER_IPV6_SUBNET,
) -> ContainerNetworkInfo:
    veth_host = container_veth_names(container_id)[0]
    return ContainerNetworkInfo(
        container_id=container_id,
        container_ip=container_ip,
        # Both subnets travel together: the v6 address is the v4 host offset rebased,
        # so deriving it from a default while the v4 came from a configured bridge
        # produces an address outside the network it claims to be on.
        container_ipv6=(
            container_ipv6_address(
                container_ip,
                ipv4_subnet=ipv4_subnet,
                ipv6_subnet=ipv6_subnet,
            )
            if ipv6_enabled
            else ""
        ),
        namespace=container_id,
        veth_host=veth_host,
        comment=container_network_comment(veth_host, container_id, container_id),
    )


def plan_network_restriction(
    *,
    block_network: bool = False,
    allow_list: list[str] | None = None,
) -> NetworkRestrictionPlan:
    allow_list = allow_list or []
    if allow_list:
        cidrs = validate_allow_list(allow_list)
        return NetworkRestrictionPlan(
            mode=NetworkRestrictionMode.Allowlist,
            apply=True,
            block_network=block_network,
            allow_list=allow_list,
            cidrs=cidrs,
        )
    if block_network:
        return NetworkRestrictionPlan(
            mode=NetworkRestrictionMode.Block,
            apply=True,
            block_network=True,
        )
    return NetworkRestrictionPlan(mode=NetworkRestrictionMode.Unrestricted, apply=False)
