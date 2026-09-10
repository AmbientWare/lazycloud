from uuid import uuid4

from control.release_settings import ReleaseSettings
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.containers.service import ContainerService
from scheduler.state import RedisSchedulerWorkerRepository
from shared.compute_policy import MachinePool
from shared.releases import ActiveRelease
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus


def select_worker_release(image: str) -> ActiveRelease:
    path = ReleaseSettings().active_file
    previous = ActiveRelease.model_validate_json(path.read_bytes())
    release = previous.model_copy(
        update={
            "generation": previous.generation + 1,
            "target": previous.target.model_copy(update={"worker_image": image}),
        }
    )
    path.write_text(release.model_dump_json())
    return release


def assign_runtime(
    containers: ContainerService, workers: RedisSchedulerWorkerRepository, container_id: str
) -> None:
    worker = workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=str(uuid4()),
            runtime_image="container-worker:local",
            pool=MachinePool("default"),
            capacity_owner_id=str(uuid4()),
            status=SchedulerWorkerStatus.Available,
        )
    )
    container = containers.get(container_id)
    ContainerSchedulingPersistenceService(
        containers.context,
        containers.events,
        containers.workspace_changes,
    ).assign_runtime(
        container_id=container_id,
        workspace_id=container.workspace_id,
        runtime_worker_id=worker.worker_id,
        runtime_machine_id="test-worker",
    )
