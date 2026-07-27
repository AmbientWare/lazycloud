from __future__ import annotations

from pathlib import Path

import pytest
from shared.container_requests import (
    RequestMount,
    RequestMountType,
    RequestVolumeStoreConfig,
)

from worker.lifecycle import (
    BindMountSourceDirAction,
    WorkerVolumeStoreUnavailableError,
    ensure_bind_mount_source_dirs,
    plan_request_mount_setup,
)

STORE = RequestVolumeStoreConfig(
    filesystem_name="lazycloud",
    root_path="/data",
    relative_path="volumes/ws-1/vol-1",
)


def _volume_mount() -> RequestMount:
    return RequestMount(
        local_path="/data/volumes/ws-1/vol-1",
        mount_path="/volumes/data",
        mount_type=RequestMountType.Volume,
        volume_config=STORE,
    )


def test_platform_volume_mount_is_refused_when_the_store_is_not_mounted() -> None:
    """A volume the worker cannot back must fail the request, not fall back to disk.

    This is the regression that shipped: the mount was planned as an ordinary
    bind, its source directory was created on ephemeral local disk, and the
    container wrote there while the client read the real store and saw nothing.
    """
    with pytest.raises(WorkerVolumeStoreUnavailableError):
        plan_request_mount_setup(
            [_volume_mount()],
            container_id="ctr-1",
            workspace_name="ws-1",
            workspace_storage_available=False,
            volume_store_available=False,
        )


def test_platform_volume_mount_never_creates_its_own_source_directory(tmp_path: Path) -> None:
    """Even when planned, a volume mount must not manufacture its bind source.

    `ensure_bind_mount_source_dirs` runs over every prepared mount, so a skip
    decision made elsewhere is not sufficient on its own.
    """
    ephemeral = tmp_path / "volumes" / "ws-1" / "vol-1"
    mount = RequestMount(
        local_path=str(ephemeral),
        mount_path="/volumes/data",
        mount_type=RequestMountType.Volume,
        volume_config=STORE,
    )

    plans = ensure_bind_mount_source_dirs([mount])

    assert [plan.action for plan in plans] == [BindMountSourceDirAction.Skip]
    assert not ephemeral.exists()


def test_platform_volume_mount_binds_the_store_path_when_available() -> None:
    """The container must bind the store-backed path the control plane named."""
    plan = plan_request_mount_setup(
        [_volume_mount()],
        container_id="ctr-1",
        workspace_name="ws-1",
        workspace_storage_available=False,
        volume_store_available=True,
    )

    assert [mount.local_path for mount in plan.mounts] == ["/data/volumes/ws-1/vol-1"]


def test_platform_volume_mount_is_refused_when_it_escapes_its_store() -> None:
    """A local path disagreeing with the declared store must not be bound."""
    mount = RequestMount(
        local_path="/data/volumes/ws-1/somewhere-else",
        mount_path="/volumes/data",
        mount_type=RequestMountType.Volume,
        volume_config=STORE,
    )

    with pytest.raises(WorkerVolumeStoreUnavailableError):
        plan_request_mount_setup(
            [mount],
            container_id="ctr-1",
            workspace_name="ws-1",
            workspace_storage_available=False,
            volume_store_available=True,
        )
