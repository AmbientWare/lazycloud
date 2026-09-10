from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from coordination.redis_client import AsyncRedisClient
from database.records.apps import StubRecord
from database.repositories.apps import StubRepository
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import ContainerRepository
from scheduler.preemption import (
    CapacityInterruption,
    SchedulerCapacityInterruptionService,
    SchedulerWorkerMaintenanceService,
    SchedulerWorkerPreemptionService,
    WorkerPreemptionOperation,
)
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
)
from scheduler.worker_rollout import WorkerWorkloadDrainService
from shared.compute_enrollment import AgentCapacityState
from shared.compute_policy import MachinePool
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployments import StubKind
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
)
from shared.tasks import Task
from sqlalchemy.engine import URL
from tests.real_redis import RealRedisActors

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

OWNER_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class _Stopper:
    calls: list[tuple[str, StopContainerReason]] = field(default_factory=list)

    def stop(self, container_id: str, *, reason: StopContainerReason) -> object:
        self.calls.append((container_id, reason))
        return object()


@dataclass(slots=True)
class _InterruptionSource:
    interruptions: list[CapacityInterruption]

    def list_active_interruptions(self) -> list[CapacityInterruption]:
        return list(self.interruptions)


def _request(container_id: str) -> SchedulerWorkerRequest:
    return SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="stub-1",
        container_id=container_id,
        cpu_millicores=1_000,
        memory_mib=512,
        timestamp=NOW,
    )


def _container(
    container_id: str,
    status: SchedulerContainerStatus,
) -> SchedulerContainerState:
    return SchedulerContainerState(
        container_id=container_id,
        stub_id="stub-1",
        workspace_id="workspace-1",
        worker_id="worker-1",
        status=status,
        scheduled_at=NOW,
    )


@pytest.mark.anyio
async def test_preemption_atomically_cordons_and_requeues_unstarted_work_once(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    stopper = _Stopper()
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id="worker-1",
            pool=MachinePool("cpu"),
            capacity_owner_id=OWNER_ID,
            machine_id="machine-1",
            status=SchedulerWorkerStatus.Available,
            runtime_classes=["runsc"],
            free_cpu_millicores=4_000,
            free_memory_mib=8_192,
            total_cpu_millicores=4_000,
            total_memory_mib=8_192,
        ),
        now=NOW,
    )
    await workers.enqueue_worker_request(async_redis, "worker-1", _request("queued"))
    await workers.enqueue_worker_request(async_redis, "worker-1", _request("cancelled"))
    # Two requests the worker acted on without acknowledging. Neither may come
    # back: the second's container has already run, and requeueing a request on
    # the strength of its container having finished runs that work twice.
    await workers.enqueue_worker_request(async_redis, "worker-1", _request("running"))
    await workers.enqueue_worker_request(async_redis, "worker-1", _request("finished"))
    containers.set_container_state(_container("queued", SchedulerContainerStatus.Pending))
    containers.set_container_state(_container("cancelled", SchedulerContainerStatus.Pending))
    containers.set_container_state(_container("running", SchedulerContainerStatus.Running))
    containers.set_container_state(_container("finished", SchedulerContainerStatus.Complete))
    containers.cancel_container_request("cancelled")
    service = SchedulerWorkerPreemptionService(workers, containers, stopper)
    interruption = CapacityInterruption(
        enrollment_id="notice-1",
        credential_generation=1,
        workspace_id="workspace-1",
        pool=MachinePool("cpu"),
        machine_id="machine-1",
        state=AgentCapacityState.Preempting,
        reason="provider interruption notice",
        observed_at=NOW,
    )
    source = _InterruptionSource([interruption])
    controller = SchedulerCapacityInterruptionService(service, workers, source)

    first = controller.preempt_interruption(interruption, now=NOW)[0]
    duplicate = controller.preempt_interruption(interruption, now=NOW)[0]
    restart = SchedulerCapacityInterruptionService(
        service,
        workers,
        source,
    ).reconcile(now=NOW)

    persisted = workers.get_worker("worker-1")
    assert persisted is not None
    assert persisted.status is SchedulerWorkerStatus.Unavailable
    assert first.changed
    assert first.requeued_request_ids == ["queued"]
    assert first.stopped_container_ids == ["running"]
    assert not duplicate.changed
    assert duplicate.requeued_request_ids == []
    assert restart[0].changed is False
    assert stopper.calls == [("running", StopContainerReason.Preempted)] * 3
    [claim] = workers.claim_ready_container_requests(now=NOW, limit=10)
    assert claim.request.container_id == "queued"
    assert claim.request.retry_count == 1


def test_preemption_rejects_stale_worker_session_fence(
    real_redis_actors: RealRedisActors,
) -> None:
    workers = RedisSchedulerWorkerRepository(real_redis_actors.client())
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id="worker-1",
            pool=MachinePool("cpu"),
            capacity_owner_id=OWNER_ID,
            machine_id="machine-1",
        ),
        now=NOW,
    )
    operation = WorkerPreemptionOperation(
        operation_id="notice-stale",
        worker_id="worker-1",
        capacity_owner_id=OWNER_ID,
        machine_id="other-machine",
        expected_resource_version=0,
        reason="stale provider interruption notice",
        observed_at=NOW,
    )

    try:
        workers.preempt_worker_requests(operation, now=NOW)
    except RuntimeError as exc:
        assert "session fence is stale" in str(exc)
    else:
        raise AssertionError("stale preemption must not mutate worker availability")
    persisted = workers.get_worker("worker-1")
    assert persisted is not None
    assert persisted.status is SchedulerWorkerStatus.Pending


def test_interruption_drains_workload_admission_until_provider_deadline(
    real_redis_actors: RealRedisActors,
    migrated_database_url: URL,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=migrated_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    workers = RedisSchedulerWorkerRepository(real_redis_actors.client())
    containers = RedisSchedulerContainerRepository(real_redis_actors.client())
    stopper = _Stopper()
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id="worker-1",
            pool=MachinePool("cpu"),
            capacity_owner_id=OWNER_ID,
            machine_id="machine-1",
            status=SchedulerWorkerStatus.Available,
        ),
        now=NOW,
    )
    try:
        workloads: list[ContainerRecord] = []
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="interruption-drain")
            for kind in (StubKind.Function, StubKind.Endpoint):
                stub = StubRepository(session).upsert(
                    StubRecord(
                        id=str(uuid4()), workspace_id=workspace.id, name=kind.value, kind=kind
                    )
                )
                container = ContainerRecord(
                    id=str(uuid4()),
                    name=kind.value,
                    image="image",
                    command=[],
                    workspace_id=workspace.id,
                    stub_id=stub.id,
                    worker_id="worker-1",
                    status=ContainerStatus.Running,
                )
                ContainerRepository(session).records.upsert(
                    container,
                    workspace_id=workspace.id,
                    name=container.name,
                    status=container.status.value,
                )
                workloads.append(container)
                containers.set_container_state(
                    _container(container.id, SchedulerContainerStatus.Running).model_copy(
                        update={"workspace_id": workspace.id, "stub_id": stub.id}
                    )
                )
            function = workloads[0]
            assert function.stub_id is not None
            task = TaskRepository(session).upsert(
                Task(
                    id=str(uuid4()),
                    name="inflight",
                    workspace_id=workspace.id,
                    stub_id=function.stub_id,
                    claimable_at=NOW,
                )
            )
            [claimed] = TaskRepository(session).claim_for_stub(
                function.stub_id, container_id=function.id, limit=1
            )
            assert claimed.id == task.id

        deadline = NOW + timedelta(minutes=2)
        interruption = CapacityInterruption(
            enrollment_id="provider-notice",
            credential_generation=1,
            workspace_id=workspace.id,
            pool=MachinePool("cpu"),
            machine_id="machine-1",
            state=AgentCapacityState.Draining,
            reason="provider interruption",
            observed_at=NOW,
            notice_at=deadline,
        )
        service = SchedulerCapacityInterruptionService(
            SchedulerWorkerPreemptionService(workers, containers, stopper),
            workers,
            source=_InterruptionSource([interruption]),
            maintenance=SchedulerWorkerMaintenanceService(workers),
            workload_drains=WorkerWorkloadDrainService(database, containers),
        )
        [drained] = service.reconcile(now=NOW)
        assert drained.worker.status is SchedulerWorkerStatus.Draining
        assert stopper.calls == []
        with database.session() as session:
            rollouts = ContainerRolloutRepository(session)
            for container in workloads:
                assert container.stub_id is not None
                assert not rollouts.accepting_work(container.id, stub_id=container.stub_id)
                assert rollouts.draining_ids([container.id]) == {container.id}
                assert rollouts.serving_floor(container.stub_id) == 1
                assert ContainerRepository(session).count_live_for_stub(container.stub_id) == 0
            assert TaskRepository(session).containers_with_inflight_work([function.id]) == {
                function.id
            }

        [before_deadline] = service.reconcile(now=deadline - timedelta(seconds=1))
        assert not before_deadline.changed
        assert stopper.calls == []

        [preempted] = service.reconcile(now=deadline)
        assert preempted.changed
        assert preempted.worker.status is SchedulerWorkerStatus.Unavailable
        assert set(stopper.calls) == {
            (container.id, StopContainerReason.Preempted) for container in workloads
        }
    finally:
        database.dispose()
