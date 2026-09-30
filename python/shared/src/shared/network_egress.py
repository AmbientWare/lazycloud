from ipaddress import ip_network
from typing import Literal

from pydantic import field_validator

from shared.contracts import ContractModel


class NetworkEgressRouteEvidence(ContractModel):
    excluded_destinations: tuple[str, ...]
    verified_ip_versions: tuple[Literal[4, 6], ...]

    @field_validator("excluded_destinations")
    @classmethod
    def validate_destinations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({str(ip_network(value)) for value in values}))
