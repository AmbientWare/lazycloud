from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    SchedulerContainerRequestClaim,
    SchedulerRepositoryError,
)
from shared.compute_policy import MachinePool
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
)
from tests.real_redis import RealRedisActors


def test_gpu_arrival_fences_backfill_before_atomic_dispatch(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    now = datetime.now(UTC)
    worker = workers.add_worker(
        SchedulerWorkerRecord(
            worker_id="gpu-worker",
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            pool=MachinePool("lazycloud"),
            status=SchedulerWorkerStatus.Available,
            request_poll_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            gpu_type="L4",
            total_gpu_count=1,
            free_gpu_count=0,
            total_cpu_millicores=8000,
            free_cpu_millicores=4000,
            total_memory_mib=16384,
            free_memory_mib=8192,
        )
    )
    cpu = SchedulerWorkerRequest(
        workspace_id="workspace",
        stub_id="stub",
        container_id="cpu",
        preemptible=True,
        cpu_millicores=1000,
        memory_mib=512,
    )
    workers.enqueue_container_request(cpu, ready_at=now)
    [claim] = workers.claim_ready_container_requests(now=now)
    containers.set_container_state(
        SchedulerContainerState(
            container_id="cpu",
            workspace_id="workspace",
            stub_id="stub",
            worker_id=worker.worker_id,
        )
    )
    workers.enqueue_container_request(
        cpu.model_copy(
            update={
                "container_id": "gpu",
                "gpu": ["nvidia-l4"],
                "gpu_count": 1,
            }
        ),
        ready_at=now,
    )
    backfill = SchedulerContainerRequestClaim(
        request=claim.request.model_copy(update={"backfill": True}),
        token=claim.token,
    )
    with pytest.raises(SchedulerRepositoryError, match="GPU demand"):
        workers.dispatch_claimed_container_request(worker.worker_id, backfill, now=now)
    unchanged = workers.get_worker(worker.worker_id)
    state = containers.get_container_state("cpu")
    assert unchanged is not None and unchanged.free_cpu_millicores == 4000
    assert state is not None and not state.backfill
    assert workers.has_recoverable_container_request("cpu")
    containers.cancel_container_request("gpu")
    workers.dispatch_claimed_container_request(worker.worker_id, backfill, now=now)
    placed = containers.get_container_state("cpu")
    assert placed is not None and placed.backfill and placed.preemptible
    assert workers.has_recoverable_container_request("cpu", worker_id=worker.worker_id)


def test_concurrent_gpu_recovery_claims_only_marked_cpu_backfill(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    now = datetime.now(UTC)
    worker = workers.add_worker(
        SchedulerWorkerRecord(
            worker_id="gpu-worker",
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            pool=MachinePool("lazycloud"),
            status=SchedulerWorkerStatus.Available,
            request_poll_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            gpu_type="l4",
            total_gpu_count=1,
            free_gpu_count=1,
            total_cpu_millicores=8000,
            free_cpu_millicores=0,
            total_memory_mib=16384,
            free_memory_mib=0,
        )
    )
    for name, backfill, gpu_count in (
        ("backfill", True, 0),
        ("ordinary", False, 0),
        ("gpu", True, 1),
    ):
        containers.set_container_state(
            SchedulerContainerState(
                container_id=name,
                workspace_id="workspace",
                stub_id="stub",
                worker_id=worker.worker_id,
                backfill=backfill,
                preemptible=True,
                gpu_count=gpu_count,
                status=SchedulerContainerStatus.Running,
            )
        )
    workers.enqueue_container_request(
        SchedulerWorkerRequest(
            workspace_id="workspace",
            stub_id="stub",
            container_id="waiting-gpu",
            gpu=["l4"],
            gpu_count=1,
        ),
        ready_at=now,
    )

    def mark() -> list[str]:
        repository = RedisSchedulerWorkerRepository(real_redis_actors.client())
        return repository.mark_gpu_backfill_evictions(
            worker,
            "waiting-gpu",
            ["backfill", "ordinary", "gpu"],
            now=now,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(mark) for _ in range(2)]
        claims = [future.result() for future in futures]
    assert sorted(claims, key=len) == [[], ["backfill"]]
    assert workers.mark_gpu_backfill_evictions(
        worker,
        "waiting-gpu",
        ["backfill", "ordinary", "gpu"],
        now=now + timedelta(seconds=61),
    ) == ["backfill"]
    for name in ("ordinary", "gpu"):
        state = containers.get_container_state(name)
        assert state is not None and not state.backfill_eviction_requested
