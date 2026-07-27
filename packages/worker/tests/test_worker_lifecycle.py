from __future__ import annotations

from shared.container_requests import RequestMount, RequestMountType

from worker.lifecycle import (
    BindMountSourceDirAction,
    plan_bind_mount_source_dirs,
    plan_request_mount_setup,
)


def _volume_mount() -> RequestMount:
    return RequestMount(
        local_path="/data/volumes/ws-1/vol-1",
        mount_path="/volumes/data",
        mount_type=RequestMountType.Volume,
    )


def test_platform_volume_resolves_into_workspace_storage() -> None:
    """A volume lives in its workspace's own storage, not a platform-wide store.

    Keeping the logical /data path would bind a directory nothing backs, which
    is how volume writes previously landed on ephemeral local disk.
    """
    plan = plan_request_mount_setup(
        [_volume_mount()],
        container_id="ctr-1",
        workspace_name="ws-1",
        workspace_storage_available=True,
    )

    assert [mount.local_path for mount in plan.mounts] == ["/workspace/ws-1/volumes/vol-1"]


def test_platform_volume_keeps_its_logical_path_without_workspace_storage() -> None:
    """Without workspace storage there is nothing to resolve onto.

    The mount must not be silently rewritten to a path that looks backed; the
    workspace storage mount is mandatory and fails the container when absent.
    """
    plan = plan_request_mount_setup(
        [_volume_mount()],
        container_id="ctr-1",
        workspace_name="ws-1",
        workspace_storage_available=False,
    )

    assert [mount.local_path for mount in plan.mounts] == ["/data/volumes/ws-1/vol-1"]


def test_volume_source_directory_is_created_inside_workspace_storage() -> None:
    """Creating the directory inside the mount materializes the object prefix.

    The prefix is lazy in object storage, so the volume's directory only exists
    once something creates it on the mounted filesystem.
    """
    resolved = RequestMount(
        local_path="/workspace/ws-1/volumes/vol-1",
        mount_path="/volumes/data",
        mount_type=RequestMountType.Volume,
    )

    plans = plan_bind_mount_source_dirs([resolved])

    assert [plan.action for plan in plans] == [BindMountSourceDirAction.Create]
