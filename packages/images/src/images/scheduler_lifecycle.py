from __future__ import annotations

from dataclasses import dataclass

from scheduler.state import ContainerStateNotFoundError, RedisSchedulerContainerRepository
from shared.scheduling import SchedulerContainerStatus

from images.lifecycle import ImageBuildContainerStateStore


@dataclass(slots=True)
class SchedulerImageBuildContainerStateStore(ImageBuildContainerStateStore):
    repository: RedisSchedulerContainerRepository

    def delete_pending_build_container(self, container_id: str) -> bool:
        return self.repository.delete_container_state(container_id)

    def mark_build_container_stopping(self, container_id: str, ttl_seconds: int) -> bool:
        try:
            result = self.repository.update_container_status(
                container_id,
                SchedulerContainerStatus.Stopping,
                ttl_seconds=ttl_seconds,
            )
        except ContainerStateNotFoundError:
            return False
        return result.changed or result.next_status is SchedulerContainerStatus.Stopping
