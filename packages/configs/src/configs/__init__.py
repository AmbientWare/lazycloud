"""Shared configuration files for LazyCloud."""

from pathlib import Path

CONFIG_DIR = Path(__file__).parent


def get_clusters_config_path() -> Path:
    """Get the path to the clusters.yaml config file."""
    return CONFIG_DIR / "clusters.yaml"
