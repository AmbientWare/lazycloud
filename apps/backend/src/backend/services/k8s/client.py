"""
Multi-cluster Kubernetes client management.

Loads kubeconfigs from AWS Secrets Manager at startup and maintains
a per-cluster client cache. Falls back to local kubeconfig for development.
"""

import asyncio
import os
import re
import tempfile
from pathlib import Path

from kubernetes_asyncio import config as async_config
from kubernetes_asyncio.client import ApiClient as AsyncApiClient
from kubernetes_asyncio.client import Configuration as AsyncConfiguration
from kubernetes_asyncio.client.api.apps_v1_api import AppsV1Api as AsyncAppsV1Api
from kubernetes_asyncio.client.api.batch_v1_api import BatchV1Api as AsyncBatchV1Api
from kubernetes_asyncio.client.api.core_v1_api import CoreV1Api as AsyncCoreV1Api
from loguru import logger
from models.clusters import get_cluster_registry
from models.k8s import PVCInfo

from backend.config import app_config
from backend.services.aws_secrets import get_secret


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


# -----------------------------------------------------------------------------
# Multi-cluster client management
# -----------------------------------------------------------------------------

_cluster_clients: dict[str, AsyncApiClient] = {}
_clients_lock = asyncio.Lock()
_initialized = False


async def initialize_cluster_clients() -> None:
    """Load kubeconfigs from Secrets Manager and create clients for all active clusters.

    In development (no AWS credentials), falls back to local kubeconfig.
    """
    global _initialized

    async with _clients_lock:
        if _initialized:
            return

        # Check if we have AWS credentials for Secrets Manager
        if app_config.AWS_ACCESS_KEY_ID and app_config.AWS_SECRET_ACCESS_KEY:
            await _load_clients_from_secrets_manager()
        else:
            # Development fallback: use local kubeconfig
            await _load_client_from_local_config()

        _initialized = True
        logger.info(f"Initialized {len(_cluster_clients)} cluster client(s)")


async def _load_clients_from_secrets_manager() -> None:
    """Load kubeconfigs for all active clusters from Secrets Manager.

    Raises:
        RuntimeError: If no active clusters could be loaded
    """
    registry = get_cluster_registry()
    active_clusters = [c for c in registry.list_clusters() if c.status == "active"]

    if not active_clusters:
        logger.warning("No active clusters found in registry")
        return

    failed_clusters: list[str] = []

    for cluster in active_clusters:
        secret_id = f"{app_config.SECRETS_PREFIX}/clusters/{cluster.name}/kubeconfig"
        kubeconfig = get_secret(secret_id)

        if not kubeconfig:
            logger.error(
                f"No kubeconfig found for cluster {cluster.name} at {secret_id}"
            )
            failed_clusters.append(cluster.name)
            continue

        try:
            client = await _create_client_from_kubeconfig(kubeconfig)
            _cluster_clients[cluster.name] = client
            logger.info(f"Loaded K8s client for cluster: {cluster.name}")
        except Exception as e:
            logger.error(f"Failed to create client for {cluster.name}: {e}")
            failed_clusters.append(cluster.name)

    # Fail if no clusters could be loaded
    if not _cluster_clients and active_clusters:
        raise RuntimeError(
            f"Failed to load any Kubernetes clients. "
            f"Attempted clusters: {[c.name for c in active_clusters]}"
        )

    if failed_clusters:
        logger.warning(
            f"Some clusters failed to load: {failed_clusters}. "
            f"Successfully loaded: {list(_cluster_clients.keys())}"
        )


async def _load_client_from_local_config() -> None:
    """Load a single client from local kubeconfig (development mode)."""
    registry = get_cluster_registry()
    default_cluster = registry.get_default_cluster()
    cluster_name = default_cluster.name if default_cluster else "default"

    try:
        # Try in-cluster config first (when running in Kubernetes)
        try:
            async_config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
            k8s_config = AsyncConfiguration.get_default_copy()
        except async_config.ConfigException:
            # Fall back to kubeconfig file
            kubeconfig_path = os.getenv("KUBECONFIG")
            if kubeconfig_path:
                await async_config.load_kube_config(config_file=kubeconfig_path)
                logger.info(f"Loaded Kubernetes configuration from {kubeconfig_path}")
            else:
                await async_config.load_kube_config()
                logger.info("Loaded Kubernetes configuration from default location")
            k8s_config = AsyncConfiguration.get_default_copy()

        k8s_config.connection_pool_maxsize = app_config.K8S_CONNECTION_POOL_SIZE
        client = AsyncApiClient(configuration=k8s_config)
        _cluster_clients[cluster_name] = client
        logger.info(f"Loaded local K8s client as cluster: {cluster_name}")

    except Exception as e:
        logger.error(f"Failed to initialize local Kubernetes client: {e}")
        raise


async def _create_client_from_kubeconfig(kubeconfig_yaml: str) -> AsyncApiClient:
    """Create an AsyncApiClient from kubeconfig YAML string.

    Note: This function modifies global kubernetes Configuration state via
    load_kube_config(). It's safe because it's only called from within
    initialize_cluster_clients() which holds _clients_lock, ensuring
    sequential execution.
    """
    # Write to temp file (kubernetes_asyncio requires file path)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(kubeconfig_yaml)
        config_path = f.name

    try:
        # load_kube_config sets the global default Configuration
        await async_config.load_kube_config(config_file=config_path)
        # get_default_copy() returns an independent copy of the current config
        k8s_config = AsyncConfiguration.get_default_copy()
        k8s_config.connection_pool_maxsize = app_config.K8S_CONNECTION_POOL_SIZE
        return AsyncApiClient(configuration=k8s_config)
    finally:
        Path(config_path).unlink(missing_ok=True)


async def get_cluster_client(cluster_id: str) -> AsyncApiClient:
    """Get the K8s client for a specific cluster.

    Args:
        cluster_id: The cluster identifier (e.g., 'ash-1')

    Returns:
        The AsyncApiClient for the specified cluster

    Raises:
        ValueError: If no client is available for the cluster
    """
    if not _initialized:
        await initialize_cluster_clients()

    if cluster_id not in _cluster_clients:
        available = list(_cluster_clients.keys())
        raise ValueError(
            f"No client available for cluster: {cluster_id}. "
            f"Available clusters: {available}"
        )

    return _cluster_clients[cluster_id]


async def get_async_api_client() -> AsyncApiClient:
    """Get the default cluster client.

    For backwards compatibility. Returns client for the default cluster
    as defined in clusters.yaml.
    """
    if not _initialized:
        await initialize_cluster_clients()

    registry = get_cluster_registry()
    default = registry.get_default_cluster()

    if default and default.name in _cluster_clients:
        return _cluster_clients[default.name]

    # Fallback to first available client
    if _cluster_clients:
        first_cluster = next(iter(_cluster_clients.keys()))
        logger.warning(f"No default cluster, using first available: {first_cluster}")
        return _cluster_clients[first_cluster]

    raise RuntimeError("No Kubernetes clients available")


# -----------------------------------------------------------------------------
# API convenience functions
# -----------------------------------------------------------------------------


async def get_async_core_v1_api(cluster_id: str | None = None) -> AsyncCoreV1Api:
    """Get async CoreV1Api client for pods, services, secrets, PVCs, etc.

    Args:
        cluster_id: Optional cluster identifier. Uses default cluster if not specified.
    """
    if cluster_id:
        client = await get_cluster_client(cluster_id)
    else:
        client = await get_async_api_client()
    return AsyncCoreV1Api(client)


async def get_async_apps_v1_api(cluster_id: str | None = None) -> AsyncAppsV1Api:
    """Get async AppsV1Api client for deployments, statefulsets, etc.

    Args:
        cluster_id: Optional cluster identifier. Uses default cluster if not specified.
    """
    if cluster_id:
        client = await get_cluster_client(cluster_id)
    else:
        client = await get_async_api_client()
    return AsyncAppsV1Api(client)


async def get_async_batch_v1_api(cluster_id: str | None = None) -> AsyncBatchV1Api:
    """Get async BatchV1Api client for jobs, cronjobs, etc.

    Args:
        cluster_id: Optional cluster identifier. Uses default cluster if not specified.
    """
    if cluster_id:
        client = await get_cluster_client(cluster_id)
    else:
        client = await get_async_api_client()
    return AsyncBatchV1Api(client)


# -----------------------------------------------------------------------------
# Lifecycle management
# -----------------------------------------------------------------------------


async def close_all_clients():
    """Close all cluster clients and cleanup resources."""
    global _initialized

    for cluster_id, client in _cluster_clients.items():
        try:
            await client.close()
            logger.debug(f"Closed client for cluster: {cluster_id}")
        except Exception as e:
            logger.warning(f"Error closing client for {cluster_id}: {e}")

    _cluster_clients.clear()
    _initialized = False
    logger.info("All Kubernetes clients closed")


# Backwards compatibility alias
async def close_async_api_client():
    """Close all clients. Alias for close_all_clients() for backwards compatibility."""
    await close_all_clients()


# -----------------------------------------------------------------------------
# PVC utilities
# -----------------------------------------------------------------------------


async def get_namespace_pvcs(
    namespace: str, cluster_id: str | None = None
) -> dict[str, str]:
    """Get existing PVCs in a namespace with their storage classes.

    Args:
        namespace: The Kubernetes namespace
        cluster_id: Optional cluster identifier

    Returns:
        Dict mapping PVC name to storage class name
    """
    try:
        core_v1 = await get_async_core_v1_api(cluster_id)
        pvcs = await core_v1.list_namespaced_persistent_volume_claim(
            namespace=namespace
        )
        return {
            pvc.metadata.name: pvc.spec.storage_class_name or "" for pvc in pvcs.items
        }
    except Exception as e:
        logger.warning(f"Failed to get PVCs for namespace {namespace}: {e}")
        return {}


async def get_namespace_pvcs_with_details(
    namespace: str, cluster_id: str | None = None
) -> list[PVCInfo]:
    """Get PVC details including size and bound PV info.

    Args:
        namespace: The Kubernetes namespace
        cluster_id: Optional cluster identifier

    Returns:
        List of PVCInfo objects with details
    """
    try:
        core_v1 = await get_async_core_v1_api(cluster_id)
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
