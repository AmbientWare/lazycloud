import os
from functools import lru_cache

from kubernetes import config
from kubernetes.client import ApiClient
from kubernetes.client.api.apps_v1_api import AppsV1Api
from kubernetes.client.api.core_v1_api import CoreV1Api
from loguru import logger


@lru_cache(maxsize=1)
def _get_api_client() -> ApiClient:
    """Get or create the Kubernetes API client."""
    try:
        # Try in-cluster config first (when running in Kubernetes)
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            # Fall back to kubeconfig file
            kubeconfig_path = os.getenv("KUBECONFIG")
            if kubeconfig_path:
                config.load_kube_config(config_file=kubeconfig_path)
                logger.info(f"Loaded Kubernetes configuration from {kubeconfig_path}")
            else:
                # Use default kubeconfig location (~/.kube/config)
                config.load_kube_config()
                logger.info("Loaded Kubernetes configuration from default location")

        return ApiClient()

    except Exception as e:
        logger.error(f"Failed to initialize Kubernetes client: {e}")
        raise


@lru_cache(maxsize=1)
def get_core_v1_api() -> CoreV1Api:
    """Get CoreV1Api client for pods, services, etc."""
    return CoreV1Api(_get_api_client())


@lru_cache(maxsize=1)
def get_apps_v1_api() -> AppsV1Api:
    """Get AppsV1Api client for deployments, statefulsets, etc."""
    return AppsV1Api(_get_api_client())
