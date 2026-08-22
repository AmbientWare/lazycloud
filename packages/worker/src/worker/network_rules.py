from __future__ import annotations

import ipaddress
import shlex

from shared.contracts import ContractModel

CONTAINER_VETH_HOST_PREFIX = "rth"


class ContainerNetworkRuleInfo(ContractModel):
    container_id: str = ""
    namespace: str = ""
    veth_host: str = ""
    ipv4: str = ""
    ipv6: str = ""


def container_network_comment(
    veth_host: str,
    container_id: str,
    namespace: str = "",
) -> str:
    return f"{veth_host}:{container_id}:{namespace or container_id}"


def container_network_rule_info_from_iptables_rule(
    rule: str,
) -> tuple[ContainerNetworkRuleInfo, bool]:
    comment = _container_network_comment_from_rule(rule)
    if not comment:
        return ContainerNetworkRuleInfo(), False
    parts = comment.split(":")
    if len(parts) < 2 or not parts[1]:
        return ContainerNetworkRuleInfo(), False

    info = ContainerNetworkRuleInfo(
        veth_host=parts[0],
        container_id=parts[1],
        namespace=parts[2] if len(parts) >= 3 and parts[2] else parts[1],
    )
    destination = iptables_rule_destination_ip(rule)
    if destination:
        try:
            parsed = ipaddress.ip_address(destination)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.version == 6:
            info.ipv6 = destination
        else:
            info.ipv4 = destination
    return info, True


def container_id_from_iptables_rule(rule: str) -> tuple[str, bool]:
    info, found = container_network_rule_info_from_iptables_rule(rule)
    return info.container_id, found


def iptables_rule_destination_ip(rule: str) -> str:
    fields = iptables_rule_fields(rule)
    for index, field in enumerate(fields):
        if field != "--to-destination" or index + 1 >= len(fields):
            continue
        return _destination_host(fields[index + 1])
    return ""


def iptables_rule_matches_source_ip(rule: str, ip: str) -> bool:
    fields = iptables_rule_fields(rule)
    for index, field in enumerate(fields[:-1]):
        if field in {"-s", "--source"} and iptables_address_matches(fields[index + 1], ip):
            return True
    return False


def iptables_rule_target(rule: str) -> str:
    fields = iptables_rule_fields(rule)
    for index, field in enumerate(fields[:-1]):
        if field in {"-j", "--jump"}:
            return fields[index + 1]
    return ""


def iptables_rule_fields(rule: str) -> list[str]:
    try:
        return shlex.split(rule)
    except ValueError:
        return [field.replace('"', "") for field in rule.split()]


def iptables_address_matches(value: str, ip: str) -> bool:
    try:
        target = ipaddress.ip_address(_strip_address(ip))
    except ValueError:
        return False
    candidate = _strip_address(value)
    try:
        network = ipaddress.ip_network(candidate, strict=False)
    except ValueError:
        try:
            return target == ipaddress.ip_address(candidate)
        except ValueError:
            return False
    return target in network


def _container_network_comment_from_rule(rule: str) -> str:
    fields = iptables_rule_fields(rule)
    for field in fields:
        cleaned = _clean_comment_field(field)
        if cleaned.startswith(CONTAINER_VETH_HOST_PREFIX):
            return cleaned
    return ""


def _clean_comment_field(value: str) -> str:
    return value.strip().strip('"').strip("'").rstrip("\\").removesuffix("*/")


def _destination_host(destination: str) -> str:
    value = destination.strip().strip('"')
    if value.startswith("["):
        end = value.find("]")
        if end > 1:
            return value[1:end]
        return ""
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    host, separator, port = value.rpartition(":")
    if separator and port.isdigit() and host:
        return host
    return value


def _strip_address(value: str) -> str:
    return value.strip().strip('"').strip("[]")
