import asyncio
import os
from functools import lru_cache

import urllib3
from kubernetes import config
from kubernetes.client import ApiClient, Configuration
from kubernetes.client.api.apps_v1_api import AppsV1Api
from kubernetes.client.api.batch_v1_api import BatchV1Api
from kubernetes.client.api.core_v1_api import CoreV1Api
from kubernetes_asyncio import config as async_config
from kubernetes_asyncio.client import ApiClient as AsyncApiClient
from kubernetes_asyncio.client import Configuration as AsyncConfiguration
from kubernetes_asyncio.client.api.core_v1_api import CoreV1Api as AsyncCoreV1Api
from loguru import logger

from lazycloud_api.config import app_config

urllib3.disable_warnings()


@lru_cache(maxsize=1)
def _get_api_client() -> ApiClient:
    """Get or create the Kubernetes API client."""
    try:
        # Try in-cluster config first (when running in Kubernetes)
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
            k8s_config = Configuration.get_default_copy()

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
            k8s_config = Configuration.get_default_copy()

        # Configure connection pool for better scaling
        k8s_config.connection_pool_maxsize = app_config.K8S_CONNECTION_POOL_SIZE

        # The timeout is handled by asyncio.wait_for wrappers in the calling code
        api_client = ApiClient(configuration=k8s_config)
        # Set a default timeout on the REST client (connect + read)
        # This is a fallback - our asyncio.wait_for calls provide the main timeout protection
        if hasattr(api_client.rest_client, "pool_manager"):
            # Configure urllib3 pool manager timeout
            api_client.rest_client.pool_manager.connection_pool_kw.setdefault(
                "timeout", 2.0
            )

        return api_client

    except Exception as e:
        logger.error(f"Failed to initialize Kubernetes client: {e}")
        raise


@lru_cache(maxsize=1)
def get_core_v1_api() -> CoreV1Api:
    """Get CoreV1Api client for pods, services, etc."""
    return CoreV1Api(_get_api_client())


@lru_cache(maxsize=1)
def get_apps_v1_api() -> AppsV1Api:
    """Get AppsV1Api client for deployments, etc."""
    return AppsV1Api(_get_api_client())


@lru_cache(maxsize=1)
def get_batch_v1_api() -> BatchV1Api:
    """Get BatchV1Api client for jobs, cronjobs, etc."""
    return BatchV1Api(_get_api_client())


# Async Kubernetes client for log streaming
_async_api_client: AsyncApiClient | None = None
_async_client_lock = asyncio.Lock()


async def get_async_api_client() -> AsyncApiClient:
    """Get or create the async Kubernetes API client."""
    global _async_api_client

    if _async_api_client is not None:
        return _async_api_client

    async with _async_client_lock:
        try:
            # Try in-cluster config first (when running in Kubernetes)
            try:
                await async_config.load_incluster_config()
                logger.info("Loaded async in-cluster Kubernetes configuration")
                k8s_config = AsyncConfiguration.get_default_copy()

            except async_config.ConfigException:
                # Fall back to kubeconfig file
                kubeconfig_path = os.getenv("KUBECONFIG")
                if kubeconfig_path:
                    await async_config.load_kube_config(config_file=kubeconfig_path)
                    logger.info(
                        f"Loaded async Kubernetes configuration from {kubeconfig_path}"
                    )

                else:
                    # Use default kubeconfig location (~/.kube/config)
                    await async_config.load_kube_config()
                    logger.info(
                        "Loaded async Kubernetes configuration from default location"
                    )

                k8s_config = AsyncConfiguration.get_default_copy()

            # Configure connection pool for better scaling
            k8s_config.connection_pool_maxsize = app_config.K8S_CONNECTION_POOL_SIZE

            _async_api_client = AsyncApiClient(configuration=k8s_config)
            return _async_api_client

        except Exception as e:
            logger.error(f"Failed to initialize async Kubernetes client: {e}")
            raise


async def get_async_core_v1_api() -> AsyncCoreV1Api:
    """Get async CoreV1Api client for pods, logs, etc."""
    api_client = await get_async_api_client()
    return AsyncCoreV1Api(api_client)


async def close_async_api_client():
    """Close the async API client and cleanup resources."""
    global _async_api_client
    if _async_api_client is not None:
        await _async_api_client.close()
        _async_api_client = None
        logger.info("Closed async Kubernetes API client")
