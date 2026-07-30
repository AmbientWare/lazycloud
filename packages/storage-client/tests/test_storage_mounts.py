from __future__ import annotations

from storage_client.mounts import (
    MountPointConfig,
    MountPointMountManager,
    StorageMountStatus,
)


def test_mount_fails_immediately_when_the_endpoint_host_does_not_resolve() -> None:
    """An unreachable endpoint must name the host instead of timing out silently.

    A mount tool given an unresolvable host neither connects nor exits, so the
    failure previously surfaced only as an anonymous timeout carrying unrelated
    startup noise.
    """
    manager = MountPointMountManager(
        config=MountPointConfig(
            bucket_name="example-bucket",
            # RFC 6761 guarantees .invalid never resolves.
            endpoint_url="http://mount-endpoint.invalid:9000",
        )
    )

    result = manager.mount("/tmp/does-not-need-to-exist")

    assert result.status is StorageMountStatus.Failed
    assert "mount-endpoint.invalid" in result.reason
    assert "does not resolve" in result.reason


def test_geesefs_data_cache_is_bounded_by_worker_memory() -> None:
    """A fixed limit claims the same RAM on every worker, which a small one cannot spare."""
    from storage_client.mounts import GEESEFS_MIN_MEMORY_LIMIT_MB, geesefs_memory_limit_mb

    # Half the worker, so the mount never takes the machine.
    assert geesefs_memory_limit_mb(configured_mb=1024, worker_memory_mib=2048) == 1024
    assert geesefs_memory_limit_mb(configured_mb=1024, worker_memory_mib=1024) == 512
    # The configured value is a ceiling, never raised by a large worker.
    assert geesefs_memory_limit_mb(configured_mb=512, worker_memory_mib=65536) == 512
    # A tiny worker still gets a usable cache, but never more than it has.
    assert geesefs_memory_limit_mb(configured_mb=1024, worker_memory_mib=64) <= 64
    assert geesefs_memory_limit_mb(configured_mb=1024, worker_memory_mib=256) == (
        GEESEFS_MIN_MEMORY_LIMIT_MB
    )
    # Unknown worker memory leaves the configured limit alone.
    assert geesefs_memory_limit_mb(configured_mb=1024, worker_memory_mib=0) == 1024
