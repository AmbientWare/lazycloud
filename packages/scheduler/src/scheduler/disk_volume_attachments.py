from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from database.repositories.disks import DiskRepository

from scheduler.services import SchedulerContext


@dataclass(frozen=True, slots=True)
class DatabaseDiskVolumeAttachments:
    """Disk volumes on each machine that no running container's placement counts.

    Read once per dispatch pass that places disks, because a volume stays on its
    machine for a while after its container stops, and a worker that restarted
    knows nothing of the volumes attached before it.
    """

    context: SchedulerContext

    def unheld_attachments(self, machine_ids: Sequence[str]) -> dict[str, int]:
        with self.context.database.session() as session:
            return DiskRepository(session).unheld_volume_attachments(machine_ids)


__all__ = ["DatabaseDiskVolumeAttachments"]
