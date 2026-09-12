from __future__ import annotations

_CLUSTER_KEY = "cluster"
_NODE_KEY = "node"


def worker_network_prefix(cluster_name: str, node_name: str) -> str:
    return ":".join(
        [
            _CLUSTER_KEY,
            _prefix_part(cluster_name),
            _NODE_KEY,
            _prefix_part(node_name),
        ]
    )


def _prefix_part(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return "unknown"
    for bad in [":", "/", "\\", " ", "\t", "\n"]:
        normalized = normalized.replace(bad, "_")
    return normalized
