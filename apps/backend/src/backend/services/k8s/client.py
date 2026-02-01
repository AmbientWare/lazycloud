"""
Multi-cluster Kubernetes client management.

Loads kubeconfigs from AWS Secrets Manager at startup and maintains
a per-cluster client cache. Falls back to local kubeconfig for development.
"""

import asyncio
import json
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
_cluster_kubeconfig_paths: dict[str, str] = {}  # cluster_id -> kubeconfig file path
_clients_lock = asyncio.Lock()
_initialized = False


def _cleanup_stale_kubeconfigs() -> None:
    """Remove any stale kubeconfig files from previous runs.

    This handles the case where the process crashed before normal cleanup,
    ensuring sensitive credentials don't persist in /tmp.
    """
    temp_dir = Path(tempfile.gettempdir())
    for kubeconfig_file in temp_dir.glob("kubeconfig-*.yaml"):
        try:
            kubeconfig_file.unlink()
            logger.debug(f"Cleaned up stale kubeconfig: {kubeconfig_file}")
        except Exception as e:
            logger.warning(
                f"Failed to clean up stale kubeconfig {kubeconfig_file}: {e}"
            )


async def initialize_cluster_clients() -> None:
    """Load kubeconfigs from Secrets Manager and create clients for all active clusters.

    In development (no AWS credentials), falls back to local kubeconfig.
    """
    global _initialized

    async with _clients_lock:
        if _initialized:
            return

        # Clean up any stale kubeconfig files from previous runs
        _cleanup_stale_kubeconfigs()

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

    Kubeconfig files are persisted to disk so both the K8s Python client
    and Helm CLI can use them.

    Raises:
        RuntimeError: If no active clusters could be loaded
    """
    registry = get_cluster_registry()
    active_clusters = [c for c in registry.clusters.values() if c.status == "active"]

    if not active_clusters:
        logger.warning("No active clusters found in registry")
        return

    failed_clusters: list[str] = []

    for cluster in active_clusters:
        # Cluster secrets are stored as JSON at {prefix}/clusters/{cluster_id}
        # with properties: kubeconfig, cloudflare_tunnel_token
        secret_id = f"{app_config.SECRETS_PREFIX}/clusters/{cluster.name}"
        cluster_secrets = get_secret(secret_id)

        if not cluster_secrets:
            logger.error(f"No secrets found for cluster {cluster.name} at {secret_id}")
            failed_clusters.append(cluster.name)
            continue

        # Parse JSON and extract kubeconfig
        try:
            secrets_data = json.loads(cluster_secrets)
            kubeconfig = secrets_data.get("kubeconfig")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse secrets JSON for {cluster.name}: {e}")
            failed_clusters.append(cluster.name)
            continue

        if not kubeconfig:
            logger.error(
                f"No kubeconfig property in secrets for cluster {cluster.name}"
            )
            failed_clusters.append(cluster.name)
            continue

        try:
            client, kubeconfig_path = await _create_client_from_kubeconfig(
                kubeconfig, cluster.name
            )
            _cluster_clients[cluster.name] = client
            _cluster_kubeconfig_paths[cluster.name] = kubeconfig_path
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
    default_cluster = registry.get_cluster_for_placement()

    if default_cluster:
        cluster_name = default_cluster.name
    else:
        # No default cluster for placement - try to find any active cluster
        active_clusters = [
            c for c in registry.clusters.values() if c.status == "active"
        ]
        if active_clusters:
            cluster_name = active_clusters[0].name
            logger.warning(
                f"No default cluster for placement, using first active cluster: {cluster_name}"
            )
        else:
            # No clusters configured at all - use fallback name
            cluster_name = "default"
            logger.warning(
                "No clusters configured in registry. Using 'default' as cluster name. "
                "Deployments must use cluster_id='default' to target this client."
            )

    # Use KUBECONFIG env var or default ~/.kube/config
    kubeconfig_path = os.getenv("KUBECONFIG") or str(Path.home() / ".kube" / "config")

    await async_config.load_kube_config(config_file=kubeconfig_path)
    logger.info(f"Loaded Kubernetes configuration from {kubeconfig_path}")

    k8s_config = AsyncConfiguration.get_default_copy()
    k8s_config.connection_pool_maxsize = app_config.K8S_CONNECTION_POOL_SIZE

    client = AsyncApiClient(configuration=k8s_config)
    _cluster_clients[cluster_name] = client
    _cluster_kubeconfig_paths[cluster_name] = kubeconfig_path

    logger.info(f"Loaded local K8s client as cluster: {cluster_name}")


async def _create_client_from_kubeconfig(
    kubeconfig_yaml: str, cluster_name: str
) -> tuple[AsyncApiClient, str]:
    """Create an AsyncApiClient from kubeconfig YAML string.

    The kubeconfig file is persisted to disk so it can be used by both
    the K8s Python client and Helm CLI.

    Note: This function modifies global kubernetes Configuration state via
    load_kube_config(). It's safe because it's only called from within
    initialize_cluster_clients() which holds _clients_lock, ensuring
    sequential execution.

    Args:
        kubeconfig_yaml: The kubeconfig content as YAML string
        cluster_name: Name of the cluster (used for file naming)

    Returns:
        Tuple of (AsyncApiClient, kubeconfig_file_path)
    """
    # Write to a named temp file that persists (for Helm CLI to use)
    config_path = Path(tempfile.gettempdir()) / f"kubeconfig-{cluster_name}.yaml"
    config_path.write_text(kubeconfig_yaml)
    # Restrict permissions to owner only
    config_path.chmod(0o600)

    # load_kube_config sets the global default Configuration
    await async_config.load_kube_config(config_file=str(config_path))
    # get_default_copy() returns an independent copy of the current config
    k8s_config = AsyncConfiguration.get_default_copy()
    k8s_config.connection_pool_maxsize = app_config.K8S_CONNECTION_POOL_SIZE

    return AsyncApiClient(configuration=k8s_config), str(config_path)


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


def get_available_cluster_ids() -> list[str]:
    """Get list of cluster IDs that have loaded K8s clients.

    Use this to filter placement candidates to only clusters we can operate on.
    """
    return list(_cluster_clients.keys())


def is_cluster_available(cluster_id: str) -> bool:
    """Check if a cluster has a loaded K8s client."""
    return cluster_id in _cluster_clients


def get_kubeconfig_path(cluster_id: str) -> str:
    """Get the kubeconfig file path for a cluster.

    This is used by Helm CLI which needs a file path to operate on a cluster.

    Args:
        cluster_id: The cluster identifier

    Returns:
        Path to the kubeconfig file

    Raises:
        ValueError: If no kubeconfig is available for the cluster
    """
    if cluster_id not in _cluster_kubeconfig_paths:
        available = list(_cluster_kubeconfig_paths.keys())
        raise ValueError(
            f"No kubeconfig available for cluster: {cluster_id}. "
            f"Available clusters: {available}"
        )
    return _cluster_kubeconfig_paths[cluster_id]


async def get_async_api_client(cluster_id: str) -> AsyncApiClient:
    """Get AsyncApiClient for a specific cluster.

    Args:
        cluster_id: The cluster identifier (required).

    Returns:
        AsyncApiClient for the specified cluster.
    """
    return await get_cluster_client(cluster_id)


# -----------------------------------------------------------------------------
# API convenience functions
# -----------------------------------------------------------------------------


async def get_async_core_v1_api(cluster_id: str) -> AsyncCoreV1Api:
    """Get async CoreV1Api client for pods, services, secrets, PVCs, etc.

    Args:
        cluster_id: The cluster identifier (required).
    """
    client = await get_cluster_client(cluster_id)
    return AsyncCoreV1Api(client)


async def get_async_apps_v1_api(cluster_id: str) -> AsyncAppsV1Api:
    """Get async AppsV1Api client for deployments, statefulsets, etc.

    Args:
        cluster_id: The cluster identifier (required).
    """
    client = await get_cluster_client(cluster_id)
    return AsyncAppsV1Api(client)


async def get_async_batch_v1_api(cluster_id: str) -> AsyncBatchV1Api:
    """Get async BatchV1Api client for jobs, cronjobs, etc.

    Args:
        cluster_id: The cluster identifier (required).
    """
    client = await get_cluster_client(cluster_id)
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

    # Clean up kubeconfig files (only temp files we created, not user's config)
    temp_dir = tempfile.gettempdir()
    for cluster_id, kubeconfig_path in _cluster_kubeconfig_paths.items():
        # Only delete files in temp directory (ones we created from Secrets Manager)
        if kubeconfig_path.startswith(temp_dir):
            try:
                Path(kubeconfig_path).unlink(missing_ok=True)
                logger.debug(f"Removed kubeconfig for cluster: {cluster_id}")
            except Exception as e:
                logger.warning(f"Error removing kubeconfig for {cluster_id}: {e}")

    _cluster_clients.clear()
    _cluster_kubeconfig_paths.clear()
    _initialized = False
    logger.info("All Kubernetes clients closed")


# Backwards compatibility alias
async def close_async_api_client():
    """Close all clients. Alias for close_all_clients() for backwards compatibility."""
    await close_all_clients()


# -----------------------------------------------------------------------------
# PVC utilities
# -----------------------------------------------------------------------------


async def get_namespace_pvcs(namespace: str, cluster_id: str) -> dict[str, str]:
    """Get existing PVCs in a namespace with their storage classes.

    Args:
        namespace: The Kubernetes namespace
        cluster_id: The cluster identifier (required)

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
    namespace: str, cluster_id: str
) -> list[PVCInfo]:
    """Get PVC details including size and bound PV info.

    Args:
        namespace: The Kubernetes namespace
        cluster_id: The cluster identifier (required)

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
                    pv = await core_v1.read_persistent_volume(name=pvc.spec.volume_name)  # type: ignore[misc]
                    if pv.spec and pv.spec.csi and pv.spec.csi.volume_handle:  # type: ignore[union-attr]
                        volume_handle = pv.spec.csi.volume_handle  # type: ignore[union-attr]
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
