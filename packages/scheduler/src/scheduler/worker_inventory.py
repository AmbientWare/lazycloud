from dataclasses import dataclass
from typing import Protocol

from database.repositories.billing_ledger import ContainerBillingShapeRepository
from database.repositories.container_scheduling import ContainerSchedulingRepository
from database.repositories.orchestration import ContainerRepository
from shared.container_requests import billable_memory_capacity
from shared.containers import ContainerStatus
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.scheduling import SchedulerContainerState, SchedulerContainerStatus

from database import DatabaseClient
from scheduler.containers import container_state_for_request


class WorkerInventoryContainers(Protocol):
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...

    def set_container_state(self, state: SchedulerContainerState) -> SchedulerContainerState: ...


@dataclass(slots=True)
class WorkerCapacityRecovery:
    database: DatabaseClient
    containers: WorkerInventoryContainers

    def restore(self, worker_id: str) -> None:
        with self.database.session() as session:
            assignments = ContainerRepository(session).list_live_runtime_assignments(worker_id)
            shapes = ContainerBillingShapeRepository(session)
            for container in assignments:
                shape = shapes.shape_for(container.id)
                if shape is None:
                    raise UpstreamUnavailableError(
                        f"worker admission cannot recover reserved resources for {container.id}"
                    )
                state = self.containers.get_container_state(container.id)
                if state is not None and state.worker_id not in {"", worker_id}:
                    raise ConflictError("worker inventory conflicts with durable assignment")
                request = ContainerSchedulingRepository(session).request_for(container.id)
                if state is None and request is not None:
                    state = container_state_for_request(request, worker_id=worker_id)
                recovered = SchedulerContainerState(
                    container_id=container.id,
                    workspace_id=container.workspace_id,
                    stub_id=container.stub_id or "",
                    worker_id=worker_id,
                    status=(
                        SchedulerContainerStatus.Running
                        if container.status is ContainerStatus.Running
                        else SchedulerContainerStatus.Pending
                    ),
                    scheduled_at=ContainerSchedulingRepository(session).assigned_at(container.id)
                    or container.created_at,
                    started_at=container.started_at,
                    cpu_millicores=shape.cpu_millicores,
                    memory_mib=billable_memory_capacity(shape.memory_mib),
                    gpu_type=shape.gpu_type,
                    gpu_count=shape.gpu_count,
                    image_id=container.image,
                )
                if state is not None:
                    recovered = state.model_copy(
                        update={
                            "worker_id": recovered.worker_id,
                            "status": recovered.status,
                            "cpu_millicores": recovered.cpu_millicores,
                            "memory_mib": recovered.memory_mib,
                            "gpu_type": recovered.gpu_type,
                            "gpu_count": recovered.gpu_count,
                            "scheduled_at": recovered.scheduled_at,
                            "started_at": recovered.started_at,
                        }
                    )
                self.containers.set_container_state(recovered)
