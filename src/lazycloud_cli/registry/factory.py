"""
Registry factory to create the appropriate registry implementation.
"""

import subprocess

from lazycloud_cli.registry.base import BaseRegistry
from lazycloud_cli.registry.default import DefaultRegistry
from lazycloud_cli.registry.minikube import MinikubeRegistry


def create_registry(registry_url: str) -> BaseRegistry:
    """
    Create the appropriate registry implementation based on the environment.

    Args:
        registry_url: The registry URL from config

    Returns:
        BaseRegistry: The appropriate registry implementation
    """
    # Check if we're in a Minikube environment
    if is_minikube_environment() and registry_url == "localhost:5000":
        return MinikubeRegistry(registry_url)

    # Future: Add ECR, GCR, etc. detection here
    # if is_ecr_registry(registry_url):
    #     return ECRRegistry(registry_url)

    # Default to standard registry
    return DefaultRegistry(registry_url)


def is_minikube_environment() -> bool:
    """Check if we're using Minikube."""
    try:
        # Check kubectl context
        result = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and "minikube" in result.stdout:
            return True

        # Also check if minikube is running
        result = subprocess.run(
            ["minikube", "status"], capture_output=True, text=True, check=False
        )
        return result.returncode == 0
    except Exception:
        return False
