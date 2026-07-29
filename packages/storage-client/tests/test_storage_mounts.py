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
