from __future__ import annotations

from pathlib import Path

import pytest
from foundation.process import ProcessResult
from worker.container_execution import ContainerExecutionContext
from worker.container_rootfs import (
    ContainerRootfsError,
    ContainerRootfsOverlayManager,
    ContainerRootfsSetupResult,
    ContainerRootfsStatus,
    ContainerRootfsSystem,
    RootfsCommandRunner,
    path_filesystem_type,
    plan_container_rootfs_overlay,
)
from worker.events import ContainerRequestContext
from worker.oci_runtime import OciRuntimeSpecBuilder
from worker.runtime_config import OciRuntimeName


def _result(argv: list[str], exit_code: int, stderr: str = "") -> ProcessResult:
    return ProcessResult(args=argv, exit_code=exit_code, stdout="", stderr=stderr)


def _ok(_timeout: float, argv: list[str]) -> ProcessResult:
    return _result(argv, 0)


def _fail(_timeout: float, argv: list[str]) -> ProcessResult:
    return _result(argv, 32, stderr="target is busy")


def _xfs_mountinfo(tmp_path: Path) -> str:
    """mountinfo where the scratch root sits on a quota-capable xfs filesystem."""
    root = str(tmp_path.resolve())
    return f"30 24 0:50 / {root} rw,relatime shared:2 - xfs /dev/nvme1n1 rw,prjquota\n"


def _manager(
    tmp_path: Path,
    *,
    mounted: bool = False,
    run: RootfsCommandRunner | None = None,
    mountinfo: str | None = None,
) -> ContainerRootfsOverlayManager:
    return ContainerRootfsOverlayManager(
        image_mount_root=tmp_path / "images",
        scratch_root=tmp_path / "container-rootfs",
        system=ContainerRootfsSystem(
            mount_checker=lambda _path: mounted,
            run_command=run if run is not None else _ok,
            read_mountinfo=lambda: _xfs_mountinfo(tmp_path) if mountinfo is None else mountinfo,
        ),
    )


def test_containers_sharing_an_image_get_separate_writable_layers(tmp_path: Path) -> None:
    """Two workspaces on one image must not share a writable filesystem."""
    image_root = tmp_path / "images"
    (image_root / "python-3-12").mkdir(parents=True)
    manager = _manager(tmp_path)

    first = manager.prepare(container_id="ctr-a", image_id="python-3-12")
    second = manager.prepare(container_id="ctr-b", image_id="python-3-12")

    assert first.status is ContainerRootfsStatus.Mounted
    assert second.status is ContainerRootfsStatus.Mounted
    assert first.upper_path != second.upper_path
    assert first.root_path != second.root_path
    # The shared image directory is the lower layer for both and is never a
    # container's writable root.
    assert str(image_root) not in {first.root_path, second.root_path}
    assert not first.root_path.startswith(str(image_root))
    assert not second.root_path.startswith(str(image_root))


def test_spec_builder_refuses_to_share_the_image_directory_as_a_writable_root(
    tmp_path: Path,
) -> None:
    """Without a prepared overlay an image-backed container must not start."""
    builder = OciRuntimeSpecBuilder(
        bundle_root=tmp_path / "bundles",
        image_mount_root=tmp_path / "images",
    )
    context = ContainerExecutionContext(
        request=ContainerRequestContext(container_id="ctr-1", image_id="image-1"),
        runtime=OciRuntimeName.Runc,
    )

    with pytest.raises(RuntimeError, match="no prepared root filesystem"):
        builder.build_spec(context, bind_ports=[], port_bindings=[])


def test_release_refuses_to_remove_the_tree_while_the_overlay_is_mounted(
    tmp_path: Path,
) -> None:
    """Removing under a live mount would delete through it into the shared image."""
    manager = _manager(tmp_path, mounted=True, run=_fail)
    container_root = tmp_path / "container-rootfs" / "ctr-1"
    (container_root / "upper").mkdir(parents=True)

    result = manager.release("ctr-1")

    assert not result.removed
    assert "failed to unmount" in result.reason
    assert container_root.exists()


def test_release_unmounts_then_removes_the_container_layer(tmp_path: Path) -> None:
    unmounted: list[list[str]] = []

    def run(_timeout: float, argv: list[str]) -> ProcessResult:
        unmounted.append(argv)
        return _result(argv, 0)

    manager = _manager(tmp_path, mounted=True, run=run)
    container_root = tmp_path / "container-rootfs" / "ctr-1"
    (container_root / "upper").mkdir(parents=True)
    (container_root / "upper" / "written.txt").write_text("payload", encoding="utf-8")

    result = manager.release("ctr-1")

    assert result.unmounted
    assert result.removed
    assert not container_root.exists()
    assert unmounted and unmounted[0][0] == "umount"


def test_overlay_backed_scratch_root_is_rejected_by_name(tmp_path: Path) -> None:
    """overlayfs cannot hold an overlay upperdir, and its own error is opaque."""
    image_root = tmp_path / "images"
    (image_root / "image-1").mkdir(parents=True)
    scratch = tmp_path / "container-rootfs"
    scratch.mkdir()

    resolved = str(scratch.resolve())
    mountinfo = f"30 24 0:50 / {resolved} rw,relatime shared:2 - overlay overlay rw\n"
    assert path_filesystem_type(scratch, mountinfo_text=mountinfo) == "overlay"

    # Provisioning is what normally rescues a non-quota root; with it off, the
    # overlay-on-overlay problem must be named rather than surfaced as a kernel error.
    manager = _manager(tmp_path, mountinfo=mountinfo)
    manager.require_quota = False

    with pytest.raises(ContainerRootfsError, match="cannot hold an overlay upperdir"):
        manager.prepare(container_id="ctr-1", image_id="image-1")


def test_unsafe_container_and_image_ids_are_rejected() -> None:
    for container_id in ("../escape", "a/b", "..", ".hidden"):
        with pytest.raises(ContainerRootfsError, match="unsafe container_id"):
            plan_container_rootfs_overlay(
                container_id=container_id,
                image_id="image-1",
                image_mount_root=Path("/mnt/images"),
                scratch_root=Path("/var/lib/lazycloud/container-rootfs"),
            )
    with pytest.raises(ContainerRootfsError, match="unsafe image_id"):
        plan_container_rootfs_overlay(
            container_id="ctr-1",
            image_id="../escape",
            image_mount_root=Path("/mnt/images"),
            scratch_root=Path("/var/lib/lazycloud/container-rootfs"),
        )


def test_request_without_an_image_keeps_its_private_bundle_rootfs(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    result = manager.prepare(container_id="ctr-1", image_id="")
    assert result.status is ContainerRootfsStatus.Skipped
    assert result.root_path == ""


def test_setup_result_reports_prepared_only_for_real_mounts() -> None:
    for status in (ContainerRootfsStatus.Mounted, ContainerRootfsStatus.AlreadyMounted):
        assert ContainerRootfsSetupResult(container_id="c", status=status).prepared
    for status in (ContainerRootfsStatus.Skipped, ContainerRootfsStatus.Failed):
        assert not ContainerRootfsSetupResult(container_id="c", status=status).prepared


def test_non_quota_host_filesystem_gets_a_quota_capable_backing_store(
    tmp_path: Path,
) -> None:
    """An ext4 host must still enforce limits, so a backing store is provisioned."""
    (tmp_path / "images" / "image-1").mkdir(parents=True)
    root = str(tmp_path.resolve())
    ext4 = f"30 24 0:50 / {root} rw,relatime shared:2 - ext4 /dev/nvme0n1p2 rw\n"
    commands: list[list[str]] = []

    def run(_timeout: float, argv: list[str]) -> ProcessResult:
        commands.append(argv)
        return _result(argv, 0)

    manager = _manager(tmp_path, mountinfo=ext4, run=run)
    result = manager.prepare(container_id="ctr-1", image_id="image-1")

    assert result.status is ContainerRootfsStatus.Mounted
    assert result.disk_limit_bytes > 0
    assert any(argv[0] == "mkfs.xfs" for argv in commands), commands
    assert any("prjquota" in part for argv in commands for part in argv), commands


def test_unquotaed_scratch_root_is_refused_when_provisioning_is_off(
    tmp_path: Path,
) -> None:
    """Never silently degrade to an unbounded container layer."""
    (tmp_path / "images" / "image-1").mkdir(parents=True)
    root = str(tmp_path.resolve())
    ext4 = f"30 24 0:50 / {root} rw,relatime shared:2 - ext4 /dev/nvme0n1p2 rw\n"
    manager = _manager(tmp_path, mountinfo=ext4)
    manager.require_quota = False

    result = manager.prepare(container_id="ctr-1", image_id="image-1")

    # With enforcement not required the container still starts, but it is
    # explicit that no limit was applied rather than reporting a false one.
    assert result.status is ContainerRootfsStatus.Mounted
    assert result.disk_limit_bytes == 0
    assert result.quota_project_id == 0


def test_requested_disk_limit_is_applied_to_the_container_layer(tmp_path: Path) -> None:
    (tmp_path / "images" / "image-1").mkdir(parents=True)
    commands: list[list[str]] = []

    def run(_timeout: float, argv: list[str]) -> ProcessResult:
        commands.append(argv)
        return _result(argv, 0)

    manager = _manager(tmp_path, run=run)
    result = manager.prepare(
        container_id="ctr-1",
        image_id="image-1",
        disk_limit_bytes=1024**3,
    )

    assert result.status is ContainerRootfsStatus.Mounted
    assert result.disk_limit_bytes == 1024**3
    assert result.quota_project_id > 0
    limit_commands = [argv for argv in commands if any("bhard=" in part for part in argv)]
    assert limit_commands, commands
    assert any(f"bhard={1024**3}" in part for part in limit_commands[0])
    # The quota is applied to the container's own upper layer, never the shared image.
    assert any(result.upper_path in part for argv in commands for part in argv)


def test_free_space_floor_refuses_new_containers(tmp_path: Path) -> None:
    """Below the reserve, admission fails by name instead of ENOSPC later."""
    (tmp_path / "images" / "image-1").mkdir(parents=True)
    manager = _manager(tmp_path)
    manager.minimum_free_bytes = 2**62

    result = manager.prepare(container_id="ctr-1", image_id="image-1")

    assert result.status is ContainerRootfsStatus.Failed
    assert "free-space reserve" in result.reason


def test_quota_project_ids_differ_between_live_containers(tmp_path: Path) -> None:
    """Two live containers must not share one quota."""
    (tmp_path / "images" / "image-1").mkdir(parents=True)
    manager = _manager(tmp_path)

    first = manager.prepare(container_id="ctr-a", image_id="image-1")
    second = manager.prepare(container_id="ctr-b", image_id="image-1")

    assert first.quota_project_id != second.quota_project_id


def test_reserve_larger_than_the_backing_store_is_rejected_at_construction() -> None:
    """A reserve the backing store can never satisfy would admit no containers."""
    with pytest.raises(ContainerRootfsError, match="must be smaller than its backing store"):
        ContainerRootfsOverlayManager(
            backing_image_bytes=1024**3,
            minimum_free_bytes=2 * 1024**3,
        )


def test_backing_store_below_the_xfs_minimum_is_rejected_by_name(tmp_path: Path) -> None:
    """mkfs.xfs reports an under-minimum size as a usage dump, so name it here."""
    (tmp_path / "images" / "image-1").mkdir(parents=True)
    manager = ContainerRootfsOverlayManager(
        image_mount_root=tmp_path / "images",
        scratch_root=tmp_path / "container-rootfs",
        backing_image_bytes=64 * 1024**2,
        minimum_free_bytes=1024,
        system=ContainerRootfsSystem(
            mount_checker=lambda _p: False,
            run_command=_ok,
            read_mountinfo=lambda: "",
        ),
    )

    with pytest.raises(ContainerRootfsError, match="too small for xfs"):
        manager.prepare(container_id="ctr-1", image_id="image-1")
