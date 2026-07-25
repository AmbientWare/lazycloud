from pathlib import Path

from cache.server import CacheDiskUsage

TEST_CACHE_DISK_BYTES = 1024 * 1024 * 1024 * 1024


def empty_cache_disk_usage(path: Path) -> CacheDiskUsage:
    del path
    return CacheDiskUsage(
        total=TEST_CACHE_DISK_BYTES,
        used=0,
        free=TEST_CACHE_DISK_BYTES,
    )
