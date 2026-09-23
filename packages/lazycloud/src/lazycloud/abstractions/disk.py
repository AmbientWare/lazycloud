from __future__ import annotations

import builtins
from collections.abc import Iterable
from dataclasses import dataclass

from shared.disks import DISK_ROOT_MOUNT_PATH, DiskMount, parse_disk_size_bytes
from shared.http.disks import DiskResponse
from shared.http.errors import HttpApiError

from lazycloud.clients.disk.control import DiskControlClient
from lazycloud.control import resolve_control_client_config


class DiskOperationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Disk:
    """A durable disk, named in the workspace, that a pod keeps across restarts.

    Mounted at ``/`` it holds the pod's whole writable root: installed packages,
    home directories and working trees all survive a stop, a redeploy, or a move
    to another machine. One container writes a disk at a time.
    """

    name: str
    size: str | int = "50Gi"
    mount_path: str = DISK_ROOT_MOUNT_PATH

    @staticmethod
    def list(*, workspace: str | None = None) -> builtins.list[DiskResponse]:
        """Every disk in the workspace."""
        client = _disk_client(workspace)
        disks: builtins.list[DiskResponse] = []
        cursor = ""
        try:
            while True:
                page = client.list(cursor=cursor)
                disks.extend(page.data)
                if not page.next:
                    return disks
                cursor = page.next
        except HttpApiError as exc:
            raise DiskOperationError(f"failed to list disks: {exc}") from exc

    def delete(self, *, workspace: str | None = None) -> None:
        """Delete this disk and everything written to it; refused while a container holds it."""
        try:
            _disk_client(workspace).delete(self.name)
        except HttpApiError as exc:
            raise DiskOperationError(f"failed to delete disk {self.name}: {exc}") from exc

    def mount(self) -> DiskMount:
        return DiskMount(
            name=self.name,
            size_bytes=parse_disk_size_bytes(self.size),
            mount_path=self.mount_path,
        )


def disk_mounts(disks: Iterable[Disk | DiskMount]) -> list[DiskMount]:
    return [disk.mount() if isinstance(disk, Disk) else disk for disk in disks]


def _disk_client(workspace: str | None) -> DiskControlClient:
    config = resolve_control_client_config(workspace=workspace)
    return DiskControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


__all__ = ["Disk", "DiskOperationError", "disk_mounts"]
