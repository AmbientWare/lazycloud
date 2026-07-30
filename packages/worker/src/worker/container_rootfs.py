"""Per-container writable root filesystem.

A materialized image directory is shared by every container that runs that image,
so it cannot also be the container's writable root: one container's writes would
be visible to every sibling, would corrupt the image for later containers, and
would outlive the container that made them. This module gives each container its
own overlay upper layer over the shared image directory, which is mounted
read-only as the lower layer.

The upper layer is also the only place a per-container disk limit can attach,
because it is the only directory whose contents belong to exactly one container.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from foundation.process import ProcessResult, run_command_with_timeout
from shared.contracts import ContractModel
from storage_client.mounts import is_mounted

DEFAULT_CONTAINER_ROOTFS_ROOT = "/var/lib/lazycloud/container-rootfs"
CONTAINER_ROOTFS_UPPER_DIR_NAME = "upper"
CONTAINER_ROOTFS_WORK_DIR_NAME = "work"
CONTAINER_ROOTFS_MERGED_DIR_NAME = "merged"
DEFAULT_CONTAINER_ROOTFS_MOUNT_TIMEOUT_SECONDS = 30.0

# overlayfs refuses to use an overlay mount as its own upperdir. A scratch root
# on overlayfs therefore fails at mount time with a message that reads like a
# kernel problem, so the filesystem is checked up front and named instead.
UNSUPPORTED_UPPER_FILESYSTEMS = frozenset({"overlay", "overlayfs"})


class ContainerRootfsStatus(StrEnum):
    Mounted = "mounted"
    AlreadyMounted = "already-mounted"
    Skipped = "skipped"
    Failed = "failed"


class ContainerRootfsSetupResult(ContractModel):
    container_id: str
    status: ContainerRootfsStatus
    root_path: str = ""
    upper_path: str = ""
    reason: str = ""

    @property
    def prepared(self) -> bool:
        return self.status in {
            ContainerRootfsStatus.Mounted,
            ContainerRootfsStatus.AlreadyMounted,
        }


class ContainerRootfsReleaseResult(ContractModel):
    container_id: str
    unmounted: bool = False
    removed: bool = False
    freed_bytes: int = 0
    reason: str = ""


class ContainerRootfsOverlayPlan(ContractModel):
    container_id: str
    lower_dir: str
    upper_dir: str
    work_dir: str
    merged_dir: str

    @property
    def mount_options(self) -> str:
        # lowerdir is the shared image directory and is never written through the
        # overlay, so the container's writes land in upperdir and the image stays
        # byte-identical for the next container that uses it.
        return f"lowerdir={self.lower_dir},upperdir={self.upper_dir},workdir={self.work_dir}"

    @property
    def mount_argv(self) -> list[str]:
        return [
            "mount",
            "-t",
            "overlay",
            "overlay",
            "-o",
            self.mount_options,
            self.merged_dir,
        ]

    @property
    def unmount_argv(self) -> list[str]:
        return ["umount", self.merged_dir]


class ContainerRootfsError(RuntimeError):
    pass


class ContainerRootfsReleaser(Protocol):
    def release(self, container_id: str) -> ContainerRootfsReleaseResult: ...


def plan_container_rootfs_overlay(
    *,
    container_id: str,
    image_id: str,
    image_mount_root: Path,
    scratch_root: Path,
) -> ContainerRootfsOverlayPlan | None:
    """Plan the overlay for one container, or None when there is no shared lower layer.

    A request without an image id gets a bundle-relative rootfs that is already
    private to the container, so it needs no overlay.
    """
    if not container_id:
        msg = "container rootfs overlay requires a container id"
        raise ContainerRootfsError(msg)
    if not image_id:
        return None
    _validate_path_segment(container_id, field="container_id")
    _validate_path_segment(image_id, field="image_id")

    container_root = scratch_root / container_id
    return ContainerRootfsOverlayPlan(
        container_id=container_id,
        lower_dir=str(image_mount_root / image_id),
        upper_dir=str(container_root / CONTAINER_ROOTFS_UPPER_DIR_NAME),
        work_dir=str(container_root / CONTAINER_ROOTFS_WORK_DIR_NAME),
        merged_dir=str(container_root / CONTAINER_ROOTFS_MERGED_DIR_NAME),
    )


def _read_proc_mountinfo() -> str:
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return ""
    return mountinfo.read_text(encoding="utf-8", errors="replace")


def path_filesystem_type(path: Path, *, mountinfo_text: str = "") -> str:
    """Filesystem type backing `path`, from the longest matching mountinfo entry."""
    if not mountinfo_text:
        mountinfo_text = _read_proc_mountinfo()

    target = _existing_ancestor(path)
    best_len = -1
    best_type = ""
    for line in mountinfo_text.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        mount_point = fields[4]
        separator = fields.index("-") if "-" in fields else -1
        if separator < 0 or len(fields) <= separator + 1:
            continue
        matches = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if matches and len(mount_point) > best_len:
            best_len = len(mount_point)
            best_type = fields[separator + 1]
    return best_type


type RootfsMountChecker = Callable[[str], bool]
type RootfsCommandRunner = Callable[[float, list[str]], ProcessResult]
type RootfsMountInfoReader = Callable[[], str]


@dataclass(slots=True)
class ContainerRootfsSystem:
    """Injectable syscall surface, mirroring `StorageMountSystem`."""

    mount_checker: RootfsMountChecker = lambda path: is_mounted(path)
    run_command: RootfsCommandRunner = lambda timeout, argv: run_command_with_timeout(timeout, argv)
    read_mountinfo: RootfsMountInfoReader = lambda: _read_proc_mountinfo()


@dataclass(slots=True)
class ContainerRootfsOverlayManager:
    """Owns per-container overlay lifetime on one worker."""

    image_mount_root: Path = Path("/mnt/images")
    scratch_root: Path = Path(DEFAULT_CONTAINER_ROOTFS_ROOT)
    system: ContainerRootfsSystem = field(default_factory=ContainerRootfsSystem)
    mount_timeout_seconds: float = DEFAULT_CONTAINER_ROOTFS_MOUNT_TIMEOUT_SECONDS
    _root_prepared: bool = field(default=False, init=False, repr=False)

    def prepare(self, *, container_id: str, image_id: str) -> ContainerRootfsSetupResult:
        plan = plan_container_rootfs_overlay(
            container_id=container_id,
            image_id=image_id,
            image_mount_root=self.image_mount_root,
            scratch_root=self.scratch_root,
        )
        if plan is None:
            return ContainerRootfsSetupResult(
                container_id=container_id,
                status=ContainerRootfsStatus.Skipped,
                reason="request has no image id, so the bundle rootfs is already private",
            )

        lower = Path(plan.lower_dir)
        if not lower.is_dir():
            return ContainerRootfsSetupResult(
                container_id=container_id,
                status=ContainerRootfsStatus.Failed,
                reason=f"image directory is not present: {plan.lower_dir}",
            )

        self._prepare_scratch_root()

        merged = Path(plan.merged_dir)
        if self.system.mount_checker(plan.merged_dir):
            return ContainerRootfsSetupResult(
                container_id=container_id,
                status=ContainerRootfsStatus.AlreadyMounted,
                root_path=plan.merged_dir,
                upper_path=plan.upper_dir,
            )

        for directory in (Path(plan.upper_dir), Path(plan.work_dir), merged):
            directory.mkdir(parents=True, exist_ok=True)

        result = self.system.run_command(self.mount_timeout_seconds, plan.mount_argv)
        if result.exit_code != 0:
            return ContainerRootfsSetupResult(
                container_id=container_id,
                status=ContainerRootfsStatus.Failed,
                reason=(
                    f"failed to mount container rootfs overlay at {plan.merged_dir}: "
                    f"{_command_failure_detail(result)}"
                ),
            )
        return ContainerRootfsSetupResult(
            container_id=container_id,
            status=ContainerRootfsStatus.Mounted,
            root_path=plan.merged_dir,
            upper_path=plan.upper_dir,
        )

    def release(self, container_id: str) -> ContainerRootfsReleaseResult:
        if not container_id:
            return ContainerRootfsReleaseResult(
                container_id=container_id,
                reason="container id is required to release a container rootfs",
            )
        _validate_path_segment(container_id, field="container_id")
        container_root = self.scratch_root / container_id
        merged = container_root / CONTAINER_ROOTFS_MERGED_DIR_NAME

        unmounted = False
        if self.system.mount_checker(str(merged)):
            result = self.system.run_command(
                self.mount_timeout_seconds,
                ["umount", str(merged)],
            )
            if result.exit_code != 0:
                # Removing the tree under a live mount would delete through it
                # into the shared image directory, so refuse rather than guess.
                return ContainerRootfsReleaseResult(
                    container_id=container_id,
                    reason=(
                        f"failed to unmount container rootfs overlay at {merged}: "
                        f"{_command_failure_detail(result)}"
                    ),
                )
            unmounted = True

        if not container_root.exists():
            return ContainerRootfsReleaseResult(
                container_id=container_id,
                unmounted=unmounted,
            )

        self._assert_owned_container_root(container_root, container_id)
        freed = _directory_allocated_bytes(container_root)
        _remove_tree(container_root)
        return ContainerRootfsReleaseResult(
            container_id=container_id,
            unmounted=unmounted,
            removed=True,
            freed_bytes=freed,
        )

    def _prepare_scratch_root(self) -> None:
        if self._root_prepared:
            return
        resolved = self.scratch_root.expanduser()
        if resolved == Path("/"):
            msg = "container rootfs scratch root cannot be the filesystem root"
            raise ContainerRootfsError(msg)
        if resolved.is_symlink():
            msg = f"container rootfs scratch root cannot be a symlink: {resolved}"
            raise ContainerRootfsError(msg)
        resolved.mkdir(parents=True, exist_ok=True)

        filesystem = path_filesystem_type(
            resolved,
            mountinfo_text=self.system.read_mountinfo(),
        )
        if filesystem in UNSUPPORTED_UPPER_FILESYSTEMS:
            # Named explicitly: overlayfs cannot stack, and the kernel's own
            # error for this is opaque.
            msg = (
                f"container rootfs scratch root {resolved} is backed by {filesystem!r}, "
                "which cannot hold an overlay upperdir; provision a dedicated "
                "filesystem for it"
            )
            raise ContainerRootfsError(msg)
        self._root_prepared = True

    def _assert_owned_container_root(self, container_root: Path, container_id: str) -> None:
        if container_root.name != container_id:
            msg = f"container rootfs path does not belong to {container_id}: {container_root}"
            raise ContainerRootfsError(msg)
        expected_parent = self.scratch_root.expanduser().resolve()
        actual_parent = container_root.parent.resolve()
        if actual_parent != expected_parent:
            msg = f"container rootfs path is outside the scratch root: {container_root}"
            raise ContainerRootfsError(msg)


def _command_failure_detail(result: ProcessResult) -> str:
    return (result.stderr or result.stdout).strip()


def _existing_ancestor(path: Path) -> str:
    candidate = path.expanduser()
    while True:
        if candidate.exists():
            return str(candidate.resolve())
        if candidate.parent == candidate:
            return str(candidate)
        candidate = candidate.parent


def _validate_path_segment(value: str, *, field: str) -> None:
    normalized = value.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or normalized.startswith(".")
    ):
        msg = f"unsafe {field}: {value!r}"
        raise ContainerRootfsError(msg)


def _directory_allocated_bytes(path: Path) -> int:
    total = 0
    try:
        total += path.lstat().st_blocks * 512
    except OSError:
        return 0
    for child in path.rglob("*"):
        try:
            total += child.lstat().st_blocks * 512
        except OSError:
            continue
    return total


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "CONTAINER_ROOTFS_MERGED_DIR_NAME",
    "CONTAINER_ROOTFS_UPPER_DIR_NAME",
    "CONTAINER_ROOTFS_WORK_DIR_NAME",
    "DEFAULT_CONTAINER_ROOTFS_MOUNT_TIMEOUT_SECONDS",
    "DEFAULT_CONTAINER_ROOTFS_ROOT",
    "UNSUPPORTED_UPPER_FILESYSTEMS",
    "ContainerRootfsError",
    "ContainerRootfsOverlayManager",
    "ContainerRootfsOverlayPlan",
    "ContainerRootfsReleaseResult",
    "ContainerRootfsReleaser",
    "ContainerRootfsSetupResult",
    "ContainerRootfsStatus",
    "ContainerRootfsSystem",
    "RootfsCommandRunner",
    "RootfsMountChecker",
    "RootfsMountInfoReader",
    "path_filesystem_type",
    "plan_container_rootfs_overlay",
]
