"""Cluster registry from YAML config.

Simple file-based cluster management. Load once at startup, reload on SIGHUP if needed.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from configs import get_clusters_config_path

ClusterStatus = Literal["active", "draining", "maintenance", "planned"]


@dataclass
class AutoscalerConfig:
    min_nodes: int
    max_nodes: int


@dataclass
class Cluster:
    name: str
    display_name: str
    provider: str
    location: str
    region: str
    max_nodes: int
    node_type: str
    network_cidr: str
    pod_cidr: str
    service_cidr: str
    tunnel_id: str
    api_endpoint: str
    status: ClusterStatus
    default: bool
    autoscaler: AutoscalerConfig

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def accepts_new_deployments(self) -> bool:
        return self.status in ("active",)


@dataclass
class Region:
    name: str
    clusters: list[str]
    default: str | None


class ClusterRegistry:
    """In-memory cluster registry loaded from YAML."""

    def __init__(self, config_path: Path | str | None = None):
        if config_path is None:
            config_path = get_clusters_config_path()
        self._config_path = Path(config_path)
        self._clusters: dict[str, Cluster] = {}
        self._regions: dict[str, Region] = {}
        self.reload()

    def reload(self) -> None:
        """Reload config from YAML file."""
        with open(self._config_path) as f:
            data = yaml.safe_load(f)

        self._clusters = {}
        for name, cfg in data.get("clusters", {}).items():
            self._clusters[name] = Cluster(
                name=name,
                display_name=cfg["display_name"],
                provider=cfg["provider"],
                location=cfg["location"],
                region=cfg["region"],
                max_nodes=cfg["max_nodes"],
                node_type=cfg["node_type"],
                network_cidr=cfg["network_cidr"],
                pod_cidr=cfg["pod_cidr"],
                service_cidr=cfg["service_cidr"],
                tunnel_id=cfg["tunnel_id"],
                api_endpoint=cfg["api_endpoint"],
                status=cfg["status"],
                default=cfg.get("default", False),
                autoscaler=AutoscalerConfig(**cfg["autoscaler"]),
            )

        self._regions = {}
        for name, cfg in data.get("regions", {}).items():
            self._regions[name] = Region(
                name=name,
                clusters=cfg.get("clusters", []),
                default=cfg.get("default"),
            )

    @property
    def clusters(self) -> dict[str, Cluster]:
        return self._clusters

    @property
    def regions(self) -> dict[str, Region]:
        return self._regions

    def get_cluster(self, name: str) -> Cluster | None:
        return self._clusters.get(name)

    def get_default_cluster(self) -> Cluster | None:
        """Get the default cluster for new deployments."""
        for cluster in self._clusters.values():
            if cluster.default and cluster.accepts_new_deployments:
                return cluster

        # Fallback to first active cluster
        for cluster in self._clusters.values():
            if cluster.accepts_new_deployments:
                return cluster

        return None

    def get_cluster_for_placement(
        self,
    ) -> Cluster | None:
        """Get best cluster for a new deployment."""
        # TODO: Implement capacity-based selection later
        return self.get_default_cluster()


# Singleton instance (lazy loaded)
_registry: ClusterRegistry | None = None


def get_cluster_registry() -> ClusterRegistry:
    """Get the cluster registry singleton."""
    global _registry
    if _registry is None:
        _registry = ClusterRegistry()
    return _registry
