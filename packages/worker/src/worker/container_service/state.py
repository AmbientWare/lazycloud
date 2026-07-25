from __future__ import annotations

from dataclasses import dataclass, field

from worker.container_service.models import WorkerContainerServiceInstance


@dataclass(slots=True)
class LocalWorkerContainerInstanceStore:
    instances: dict[str, WorkerContainerServiceInstance] = field(default_factory=dict)

    def get_container_instance(
        self,
        container_id: str,
    ) -> WorkerContainerServiceInstance | None:
        return self.instances.get(container_id)

    def save_container_instance(self, instance: WorkerContainerServiceInstance) -> None:
        self.instances[instance.container_id] = instance

    def delete_container_instance(self, container_id: str) -> bool:
        return self.instances.pop(container_id, None) is not None

    def list_container_instances(self) -> list[WorkerContainerServiceInstance]:
        return [self.instances[key] for key in sorted(self.instances)]
