from __future__ import annotations

from pathlib import Path

from worker.disk_volumes import DiskVolumeMounts


def test_unmounting_a_volume_that_was_never_mounted_clears_engine_leftovers(
    tmp_path: Path,
) -> None:
    """Release unmounts before giving the lease back, and the engine creates its
    lock directory under the root even when nothing is mounted there. A leftover
    it cannot remove would fail every retried release and pin the lease."""
    volumes = DiskVolumeMounts(mount_root=tmp_path / "vol", device_root=tmp_path / "dev")
    root = volumes.root("disk-1")
    (root / ".locks").mkdir(parents=True)
    (root / ".locks" / "disk-1").touch()

    volumes.unmount("disk-1")
    volumes.unmount("disk-1")

    assert not root.exists()
