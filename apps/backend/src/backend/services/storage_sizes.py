"""Storage size fetching with caching."""

from backend.database import get_db_context
from backend.services.cache import get_cache_service

# Cache key prefix and TTL (15 minutes = billing collection interval)
STORAGE_SIZE_CACHE_PREFIX = "storage_sizes:"
STORAGE_SIZE_TTL_SECONDS = 900


async def get_storage_sizes_cached(
    deployment_id: str,
) -> dict[str, tuple[str, float]]:
    """Get storage sizes for a deployment, using cache when available.

    Returns a dict mapping volume name to (storage_class, size_gb).
    Results are cached for 15 minutes (matching billing collection interval).
    """
    cache = get_cache_service()
    cache_key = f"{STORAGE_SIZE_CACHE_PREFIX}{deployment_id}"

    # Try cache first
    cached = await cache.get(cache_key)
    if cached is not None:
        # Convert list back to tuple for each value
        return {k: tuple(v) for k, v in cached.items()}

    # Fetch from database
    async with get_db_context() as db:
        storage_sizes = await db.usage.get_latest_storage_sizes(deployment_id)

    # Cache the result (convert tuples to lists for JSON serialization)
    # Always cache, even if empty, to avoid repeated DB queries
    cacheable = {k: list(v) for k, v in storage_sizes.items()}
    await cache.set(cache_key, cacheable, STORAGE_SIZE_TTL_SECONDS)

    return storage_sizes
