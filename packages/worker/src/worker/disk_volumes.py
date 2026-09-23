"""The provider volume each disk keeps its layers on, on a provider machine.

The control plane attaches one block volume per disk to the machine that holds
it and names it by the provider's volume id. The worker finds the device that
id belongs to, formats it on first use, and mounts it at a directory of the
disk's own, which the disk engine then uses as that disk's root. A disk's files
therefore live only on its volume, and the space it can use is the volume's.

The worker container sees the host's sysfs but not devices that appear after it
started, so the device is found through sysfs and given a node of its own here
rather than looked up under ``/dev``.
"""

from __future__ import annotations

import logging
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from foundation.process import ProcessResult, run_command_with_timeout

from worker.durable_disk_records import DiskBlockVolume

LOGGER = logging.getLogger(__name__)

DEFAULT_DISK_VOLUME_MOUNT_ROOT = "/run/lazycloud/vol"
"""Short, because the engine's sockets sit two disk ids below it and a unix
socket path holds 107 bytes."""

DEFAULT_DISK_VOLUME_DEVICE_ROOT = "/run/lazycloud/volume-devices"
DISK_VOLUME_APPEAR_SECONDS = 60.0
"""How long an attached volume may take to show up as a block device."""

DISK_VOLUME_POLL_SECONDS = 0.5
_SYS_BLOCK = Path("/sys/block")
_EXT4_MAGIC_OFFSET = 1080
_EXT4_MAGIC = b"\x53\xef"
_MKFS_TIMEOUT_SECONDS = 600.0
_MOUNT_TIMEOUT_SECONDS = 60.0
_RESIZE_TIMEOUT_SECONDS = 600.0

type VolumeCommandRunner = Callable[[float, list[str]], ProcessResult]


class DiskVolumeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BlockDevice:
    name: str
    major: int
    minor: int
    size_bytes: int


def volume_device_serial(volume_id: str) -> str:
    """The serial an attached volume's block device reports for its id.

    NVMe-attached volumes report the id without its dash, so ``vol-0abc``
    appears as ``vol0abc``.
    """
    return volume_id.replace("-", "")


def find_volume_device(volume_id: str, *, sys_block: Path = _SYS_BLOCK) -> BlockDevice | None:
    """The block device of an attached volume, or None while it has not appeared."""
    serial = volume_device_serial(volume_id)
    for entry in sorted(sys_block.iterdir()):
        try:
            reported = (entry / "device" / "serial").read_text(encoding="ascii").strip()
        except OSError:
            continue
        if reported != serial:
            continue
        major, minor = (entry / "dev").read_text(encoding="ascii").strip().split(":")
        sectors = int((entry / "size").read_text(encoding="ascii").strip())
        return BlockDevice(
            name=entry.name, major=int(major), minor=int(minor), size_bytes=sectors * 512
        )
    return None


def mounted_paths() -> set[Path]:
    """Mount points in this process's mount namespace."""
    points: set[Path] = set()
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) > 4:
                points.add(Path(fields[4].encode().decode("unicode_escape")))
    return points


@dataclass(slots=True)
class DiskVolumeMounts:
    """Finds, formats, and mounts each disk's volume at the disk's own root."""

    mount_root: Path = Path(DEFAULT_DISK_VOLUME_MOUNT_ROOT)
    device_root: Path = Path(DEFAULT_DISK_VOLUME_DEVICE_ROOT)
    sys_block: Path = _SYS_BLOCK
    appear_seconds: float = DISK_VOLUME_APPEAR_SECONDS
    run_command: VolumeCommandRunner = field(
        default=lambda timeout, argv: run_command_with_timeout(timeout, argv)
    )
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic

    def root(self, disk_id: str) -> Path:
        return self.mount_root / disk_id

    def mount(self, disk_id: str, volume: DiskBlockVolume, *, min_size_bytes: int) -> Path:
        """Mount the disk's volume at its root, formatting it if it is new.

        Idempotent: a volume already mounted there is only grown to fill a
        device the provider enlarged.
        """
        root = self.root(disk_id)
        device = self._await_device(volume.volume_id)
        if device.size_bytes < min_size_bytes:
            raise DiskVolumeError(
                f"volume {volume.volume_id} for disk {disk_id} is {device.size_bytes} bytes; "
                f"the disk needs a volume of at least {min_size_bytes}"
            )
        node = self._device_node(volume.volume_id, device)
        if root not in mounted_paths():
            has_filesystem = _holds_ext4(node)
            if not has_filesystem:
                if volume.formatted:
                    raise DiskVolumeError(
                        f"volume {volume.volume_id} for disk {disk_id} was formatted before "
                        "but holds no ext4 filesystem"
                    )
                # No reserved blocks and few inodes: only the engine writes
                # here, a handful of large files, and the headroom is sized
                # for layer data rather than filesystem overhead.
                self._run(
                    _MKFS_TIMEOUT_SECONDS,
                    "mkfs.ext4",
                    "-q",
                    "-F",
                    "-m",
                    "0",
                    "-T",
                    "largefile4",
                    str(node),
                )
                LOGGER.info("formatted volume %s for disk %s", volume.volume_id, disk_id)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._run(
                _MOUNT_TIMEOUT_SECONDS, "mount", "-t", "ext4", "-o", "noatime", str(node), str(root)
            )
        # Online, so it needs no fsck; a filesystem that already fills its
        # device is left as it is.
        self._run(_RESIZE_TIMEOUT_SECONDS, "resize2fs", str(node))
        return root

    def remount(self, disk_id: str, volume_id: str) -> Path | None:
        """Mount a volume a previous worker process mounted, after a restart.

        None when the device is gone or never received a filesystem, since then
        nothing of the disk is on it.
        """
        root = self.root(disk_id)
        if root in mounted_paths():
            return root
        device = find_volume_device(volume_id, sys_block=self.sys_block)
        if device is None:
            LOGGER.warning(
                "volume %s for disk %s is no longer attached to this machine", volume_id, disk_id
            )
            return None
        node = self._device_node(volume_id, device)
        if not _holds_ext4(node):
            return None
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._run(
            _MOUNT_TIMEOUT_SECONDS, "mount", "-t", "ext4", "-o", "noatime", str(node), str(root)
        )
        return root

    def unmount(self, disk_id: str) -> None:
        """Unmount the disk's volume so the control plane can detach it."""
        root = self.root(disk_id)
        if root in mounted_paths():
            self._run(_MOUNT_TIMEOUT_SECONDS, "umount", str(root))
        if root.exists():
            root.rmdir()

    def unmount_all_except(self, disk_ids: set[str]) -> None:
        """Unmount volumes no held disk needs, left by a process that died releasing them."""
        for point in mounted_paths():
            if point.parent == self.mount_root and point.name not in disk_ids:
                LOGGER.warning("unmounting volume of disk %s, which no lease holds", point.name)
                self.unmount(point.name)

    def free_bytes(self, disk_id: str) -> int:
        usage = os.statvfs(self.root(disk_id))
        return usage.f_bavail * usage.f_frsize

    def _await_device(self, volume_id: str) -> BlockDevice:
        deadline = self.monotonic() + self.appear_seconds
        while True:
            device = find_volume_device(volume_id, sys_block=self.sys_block)
            if device is not None:
                return device
            if self.monotonic() >= deadline:
                raise DiskVolumeError(
                    f"volume {volume_id} did not appear as a block device within "
                    f"{self.appear_seconds:.0f}s"
                )
            self.sleep(DISK_VOLUME_POLL_SECONDS)

    def _device_node(self, volume_id: str, device: BlockDevice) -> Path:
        self.device_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        node = self.device_root / volume_id
        rdev = os.makedev(device.major, device.minor)
        try:
            existing = node.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISBLK(existing.st_mode) and existing.st_rdev == rdev:
                return node
            node.unlink()
        os.mknod(node, stat.S_IFBLK | 0o600, rdev)
        return node

    def _run(self, timeout_seconds: float, *argv: str) -> str:
        result = self.run_command(timeout_seconds, list(argv))
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip()
            raise DiskVolumeError(f"{' '.join(argv)} failed: {detail}")
        return result.stdout


def _holds_ext4(node: Path) -> bool:
    with open(node, "rb") as handle:
        handle.seek(_EXT4_MAGIC_OFFSET)
        return handle.read(len(_EXT4_MAGIC)) == _EXT4_MAGIC


__all__ = [
    "DEFAULT_DISK_VOLUME_DEVICE_ROOT",
    "DEFAULT_DISK_VOLUME_MOUNT_ROOT",
    "BlockDevice",
    "DiskVolumeError",
    "DiskVolumeMounts",
    "find_volume_device",
    "volume_device_serial",
]
