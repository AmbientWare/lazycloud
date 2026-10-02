from __future__ import annotations

import builtins
from collections.abc import Iterable
from dataclasses import dataclass

from lazycloud._shared.disks import DISK_ROOT_MOUNT_PATH, DiskMount, parse_disk_size_bytes
from lazycloud.clients.api import ApiConnectionError, ApiError
from lazycloud.clients.storage import StorageClient
from lazycloud.contracts import api
from lazycloud.control import resolve_control_client_config, storage_client


class DiskOperationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Disk:
    """A durable disk, named in the workspace, that a pod keeps across restarts.

    Mounted at ``/`` it holds the pod's whole writable root: installed packages,
    home directories and working trees all survive a stop, a redeploy, or a move
    to another machine. One container writes a disk at a time. Sizes run from
    1Gi to 1Ti in whole 4096-byte blocks.
    """

    name: str
    size: str | int = "50Gi"
    mount_path: str = DISK_ROOT_MOUNT_PATH

    @staticmethod
    def list(*, workspace: str | None = None) -> builtins.list[api.Disk]:
        """Every disk in the workspace, by name."""
        client = _storage(workspace)
        disks: builtins.list[api.Disk] = []
        cursor: str | None = None
        try:
            while True:
                page = client.list_disks(cursor=cursor)
                disks.extend(page.disks)
                if not page.next_cursor:
                    return disks
                cursor = page.next_cursor
        except (ApiError, ApiConnectionError) as exc:
            raise DiskOperationError(f"failed to list disks: {exc}") from exc

    def delete(self, *, workspace: str | None = None) -> None:
        """Delete this disk and everything written to it; refused while a container holds it."""
        try:
            _storage(workspace).delete_disk(self.name)
        except (ApiError, ApiConnectionError) as exc:
            raise DiskOperationError(f"failed to delete disk {self.name}: {exc}") from exc

    def mount(self) -> DiskMount:
        return DiskMount(
            name=self.name,
            size_bytes=parse_disk_size_bytes(self.size),
            mount_path=self.mount_path,
        )


def disk_mounts(disks: Iterable[Disk | DiskMount]) -> list[DiskMount]:
    return [disk.mount() if isinstance(disk, Disk) else disk for disk in disks]


def _storage(workspace: str | None) -> StorageClient:
    return storage_client(resolve_control_client_config(workspace=workspace))


__all__ = ["Disk", "DiskOperationError", "disk_mounts"]
