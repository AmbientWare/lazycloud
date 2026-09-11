from datetime import timedelta
from uuid import uuid4

from api.server.services import ApiServices
from database.repositories.billing_ledger import ContainerBillingShapeRepository
from database.repositories.orchestration import ContainerRepository
from scheduler.state import RedisSchedulerContainerRepository, RedisSchedulerWorkerRepository
from scheduler.worker_inventory import WorkerCapacityRecovery
from scheduler.worker_rollout import worker_rollout_allowance
from shared.billing_quotes import ContainerShape
from shared.compute_policy import MachinePool
from shared.containers import ContainerRecord, ContainerStatus
from shared.scheduling import SchedulerContainerStatus, SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner
from tests.real_redis import RealRedisActors


def test_worker_recovery_reserves_durable_running_capacity_before_admission(
    isolated_services: ApiServices, real_redis_actors: RealRedisActors
) -> None:
    database = isolated_services.context.database
    container_id = str(uuid4())
    worker_id = str(uuid4())
    with database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                workspace_id=workspace_id,
                name="resident",
                image="",
                command=[],
                runtime_worker_id=worker_id,
                status=ContainerStatus.Running,
            )
        )
        ContainerBillingShapeRepository(session).record(
            container_id=container_id,
            workspace_id=workspace_id,
            shape=ContainerShape(
                billing_owner=UsageBillingOwner.PlatformFleet,
                gpu_type="L4",
                gpu_count=1,
                cpu_millicores=1000,
                memory_mib=1280,
            ),
        )
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=worker_id,
            pool=MachinePool("fleet"),
            capacity_owner_id=str(uuid4()),
            status=SchedulerWorkerStatus.Pending,
            total_cpu_millicores=4000,
            total_memory_mib=8192,
            total_gpu_count=2,
            free_cpu_millicores=4000,
            free_memory_mib=8192,
            free_gpu_count=2,
        )
    )
    recovery = WorkerCapacityRecovery(database, containers)
    recovery.restore(worker_id)
    recovery.restore(worker_id)
    worker = workers.toggle_worker_available(worker_id)
    assert worker.free_cpu_millicores == 3000
    assert worker.free_memory_mib == 6912
    assert worker.free_gpu_count == 1
    state = containers.get_container_state(container_id)
    assert state is not None and state.status is SchedulerContainerStatus.Running
    assert state.worker_id == worker_id
    assert state.memory_mib == 1024


def test_platform_rollout_preserves_request_intake_across_pools() -> None:
    now = utc_now()
    current = SchedulerWorkerRecord(
        worker_id="current",
        pool=MachinePool("fleet"),
        capacity_owner_id=str(uuid4()),
        billing_owner=UsageBillingOwner.PlatformFleet,
        status=SchedulerWorkerStatus.Available,
        request_poll_expires_at=now + timedelta(seconds=30),
    )
    other = current.model_copy(update={"worker_id": "other", "capacity_owner_id": str(uuid4())})
    assert worker_rollout_allowance(current, [current, other], now=now) == 1
    stale = other.model_copy(update={"request_poll_expires_at": now - timedelta(seconds=1)})
    assert worker_rollout_allowance(current, [current, stale], now=now) == 0
    assert worker_rollout_allowance(current, [current], now=now) == 0
    assert worker_rollout_allowance(stale, [stale], now=now) == 1
