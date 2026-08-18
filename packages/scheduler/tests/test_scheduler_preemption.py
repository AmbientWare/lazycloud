from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from coordination.redis_client import RedisClient
from scheduler.preemption import (
    CapacityInterruption,
    SchedulerCapacityInterruptionService,
    SchedulerWorkerPreemptionService,
    WorkerPreemptionOperation,
)
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
)
from shared.compute_enrollment import AgentCapacityState
from shared.compute_policy import MachinePool
from shared.container_requests import StopContainerReason
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
)

OWNER_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


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


def test_preemption_atomically_cordons_and_requeues_unstarted_work_once(
    real_redis_actors: _RealRedisActors,
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
    workers.enqueue_worker_request("worker-1", _request("queued"))
    workers.enqueue_worker_request("worker-1", _request("cancelled"))
    # Two requests the worker acted on without acknowledging. Neither may come
    # back: the second's container has already run, and requeueing a request on
    # the strength of its container having finished runs that work twice.
    workers.enqueue_worker_request("worker-1", _request("running"))
    workers.enqueue_worker_request("worker-1", _request("finished"))
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
    real_redis_actors: _RealRedisActors,
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
