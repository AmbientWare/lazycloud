from __future__ import annotations

from shared.contracts import ContractModel

_CLUSTER_KEY = "cluster"
_NODE_KEY = "node"


class WorkerNetworkScope(ContractModel):
    cluster_name: str
    node_name: str


def worker_network_prefix(cluster_name: str, node_name: str) -> str:
    return ":".join(
        [
            _CLUSTER_KEY,
            _prefix_part(cluster_name),
            _NODE_KEY,
            _prefix_part(node_name),
        ]
    )


def parse_worker_network_prefix(prefix: str) -> WorkerNetworkScope | None:
    parts = prefix.strip().split(":")
    if len(parts) < 4 or len(parts) % 2 != 0 or parts[0] != _CLUSTER_KEY:
        return None
    pairs = dict(zip(parts[0::2], parts[1::2], strict=False))
    cluster_name = pairs.get(_CLUSTER_KEY, "")
    node_name = pairs.get(_NODE_KEY, "")
    if not cluster_name or not node_name:
        return None
    return WorkerNetworkScope(cluster_name=cluster_name, node_name=node_name)


def normalize_worker_network_prefix(cluster_name: str, network_prefix: str) -> str:
    parsed = parse_worker_network_prefix(network_prefix)
    if parsed is not None:
        return worker_network_prefix(parsed.cluster_name, parsed.node_name)
    return worker_network_prefix(cluster_name, network_prefix)


def _prefix_part(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return "unknown"
    for bad in [":", "/", "\\", " ", "\t", "\n"]:
        normalized = normalized.replace(bad, "_")
    return normalized
