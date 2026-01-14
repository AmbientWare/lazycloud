import asyncio
import os
import re

from kubernetes_asyncio import config as async_config
from kubernetes_asyncio.client import ApiClient as AsyncApiClient
from kubernetes_asyncio.client import Configuration as AsyncConfiguration
from kubernetes_asyncio.client.api.apps_v1_api import AppsV1Api as AsyncAppsV1Api
from kubernetes_asyncio.client.api.batch_v1_api import BatchV1Api as AsyncBatchV1Api
from kubernetes_asyncio.client.api.core_v1_api import CoreV1Api as AsyncCoreV1Api
from loguru import logger
from models.k8s import PVCInfo

from backend.config import app_config


def parse_k8s_size_to_gb(size_str: str) -> float:
    """Parse Kubernetes size string (e.g., '10Gi', '500Mi') to GB."""
    if not size_str:
        return 0.0

    match = re.match(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]*)$", size_str.strip())
    if not match:
        logger.warning(f"Could not parse size string: {size_str}")
        return 0.0

    value = float(match.group(1))
    unit = match.group(2).lower() if match.group(2) else ""

    # Binary units (powers of 1024)
    if unit in ("gi", "gib"):
        return value * (1024**3) / (1000**3)  # Convert GiB to GB
    elif unit in ("mi", "mib"):
        return value * (1024**2) / (1000**3)  # Convert MiB to GB
    elif unit in ("ki", "kib"):
        return value * 1024 / (1000**3)  # Convert KiB to GB
    elif unit in ("ti", "tib"):
        return value * (1024**4) / (1000**3)  # Convert TiB to GB
    # Decimal units (powers of 1000)
    elif unit == "g":
        return value
    elif unit == "m":
        return value / 1000
    elif unit == "k":
        return value / (1000**2)
    elif unit == "t":
        return value * 1000
    elif unit == "":
        # Assume bytes
        return value / (1000**3)
    else:
        logger.warning(f"Unknown size unit: {unit}")
        return value


# Async Kubernetes client singleton
_async_api_client: AsyncApiClient | None = None
_async_client_lock = asyncio.Lock()


async def get_async_api_client() -> AsyncApiClient:
    """Get or create the async Kubernetes API client."""
    global _async_api_client

    if _async_api_client is not None:
        return _async_api_client

    async with _async_client_lock:
        # Double-check after acquiring lock
        if _async_api_client is not None:
            return _async_api_client

        try:
            # Try in-cluster config first (when running in Kubernetes)
            try:
                # load_incluster_config is synchronous in kubernetes_asyncio
                async_config.load_incluster_config()
                logger.info("Loaded in-cluster Kubernetes configuration")
                k8s_config = AsyncConfiguration.get_default_copy()

            except async_config.ConfigException:
                # Fall back to kubeconfig file
                kubeconfig_path = os.getenv("KUBECONFIG")
                if kubeconfig_path:
                    await async_config.load_kube_config(config_file=kubeconfig_path)
                    logger.info(
                        f"Loaded Kubernetes configuration from {kubeconfig_path}"
                    )

                else:
                    # Use default kubeconfig location (~/.kube/config)
                    await async_config.load_kube_config()
                    logger.info("Loaded Kubernetes configuration from default location")

                k8s_config = AsyncConfiguration.get_default_copy()

            # Configure connection pool for better scaling
            k8s_config.connection_pool_maxsize = app_config.K8S_CONNECTION_POOL_SIZE

            _async_api_client = AsyncApiClient(configuration=k8s_config)
            return _async_api_client

        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}")
            raise


async def get_async_core_v1_api() -> AsyncCoreV1Api:
    """Get async CoreV1Api client for pods, services, secrets, PVCs, etc."""
    api_client = await get_async_api_client()
    return AsyncCoreV1Api(api_client)


async def get_async_apps_v1_api() -> AsyncAppsV1Api:
    """Get async AppsV1Api client for deployments, etc."""
    api_client = await get_async_api_client()
    return AsyncAppsV1Api(api_client)


async def get_async_batch_v1_api() -> AsyncBatchV1Api:
    """Get async BatchV1Api client for jobs, cronjobs, etc."""
    api_client = await get_async_api_client()
    return AsyncBatchV1Api(api_client)


async def close_async_api_client():
    """Close the async API client and cleanup resources."""
    global _async_api_client
    if _async_api_client is not None:
        await _async_api_client.close()
        _async_api_client = None
        logger.info("Closed Kubernetes API client")


async def get_namespace_pvcs(namespace: str) -> dict[str, str]:
    """Get existing PVCs in a namespace with their storage classes.

    Returns:
        Dict mapping PVC name to storage class name
    """
    try:
        core_v1 = await get_async_core_v1_api()
        pvcs = await core_v1.list_namespaced_persistent_volume_claim(
            namespace=namespace
        )
        return {
            pvc.metadata.name: pvc.spec.storage_class_name or "" for pvc in pvcs.items
        }
    except Exception as e:
        logger.warning(f"Failed to get PVCs for namespace {namespace}: {e}")
        return {}


async def get_namespace_pvcs_with_details(namespace: str) -> list[PVCInfo]:
    """Get PVC details including size and bound PV info."""
    try:
        core_v1 = await get_async_core_v1_api()
        pvcs = await core_v1.list_namespaced_persistent_volume_claim(
            namespace=namespace
        )

        pvc_infos = []
        for pvc in pvcs.items:
            # Get requested size from PVC spec
            size_str = ""
            if pvc.spec.resources and pvc.spec.resources.requests:
                size_str = pvc.spec.resources.requests.get("storage", "")

            requested_size_gb = parse_k8s_size_to_gb(size_str)

            # Get volume handle from bound PV (for EFS file system ID)
            volume_handle = None
            if pvc.spec.volume_name:
                try:
                    pv = await core_v1.read_persistent_volume(name=pvc.spec.volume_name)
                    if pv.spec.csi and pv.spec.csi.volume_handle:
                        volume_handle = pv.spec.csi.volume_handle
                except Exception as e:
                    logger.debug(f"Could not get PV for {pvc.spec.volume_name}: {e}")

            pvc_infos.append(
                PVCInfo(
                    name=pvc.metadata.name,
                    storage_class=pvc.spec.storage_class_name or "",
                    requested_size_gb=requested_size_gb,
                    volume_handle=volume_handle,
                )
            )

        return pvc_infos

    except Exception as e:
        logger.warning(f"Failed to get PVC details for namespace {namespace}: {e}")
        return []
