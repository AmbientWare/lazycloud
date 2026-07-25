from __future__ import annotations

from ipaddress import ip_network

from shared.contracts import ContractModel

MAX_ALLOW_LIST_ENTRIES = 10


class CidrValidation(ContractModel):
    normalized: str
    is_ipv6: bool


def validate_cidr(entry: str) -> CidrValidation:
    try:
        network = ip_network(entry, strict=False)
    except ValueError as exc:
        msg = "not a valid CIDR notation"
        raise ValueError(msg) from exc
    return CidrValidation(normalized=str(network), is_ipv6=network.version == 6)


def validate_allow_list(
    allow_list: list[str],
    *,
    max_entries: int = MAX_ALLOW_LIST_ENTRIES,
) -> list[CidrValidation]:
    if len(allow_list) > max_entries:
        msg = f"allowlist exceeds maximum of {max_entries} entries"
        raise ValueError(msg)
    return [validate_cidr(entry) for entry in allow_list]
