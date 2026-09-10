from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Never

import pytest
from coordination.redis_client import AsyncRedisClient, RedisClient
from scheduler.state import (
    ConcurrencyReservationStatus,
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    SchedulerContainerRequestClaim,
    SchedulerRepositoryError,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
)
from shared.compute_policy import MachinePool
from shared.placement import ProductRegion
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis


class _FailingEvalRedis(FakeRedis):
    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | bytes | int | float | bool,
    ) -> Never:
        _ = script, numkeys, keys_and_args
        raise OSError("Redis transport unavailable")


def _request(container_id: str, *, now: datetime) -> SchedulerWorkerRequest:
    return SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="stub-1",
        container_id=container_id,
        cpu_millicores=100,
        memory_mib=128,
        timestamp=now,
    )


def _available_worker(worker_id: str, *, now: datetime) -> SchedulerWorkerRecord:
    return SchedulerWorkerRecord(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        worker_id=worker_id,
        pool=MachinePool("default"),
        status=SchedulerWorkerStatus.Available,
        free_cpu_millicores=10_000,
        free_memory_mib=10_000,
        total_cpu_millicores=10_000,
        total_memory_mib=10_000,
        created_at=now,
        updated_at=now,
    )


def test_script_transport_failure_propagates_without_non_atomic_fallback() -> None:
    repository = RedisSchedulerWorkerRepository(
        RedisClient(_FailingEvalRedis(), key_prefix="redis-failure")
    )
    request = _request("transport-failure", now=datetime(2026, 1, 1, tzinfo=UTC))

    with pytest.raises(OSError, match="Redis transport unavailable"):
        repository.enqueue_container_request(request)


def test_region_change_before_dispatch_preserves_claim_and_capacity(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    repository = RedisSchedulerWorkerRepository(real_redis_actors.client())
    worker = _available_worker("regional-worker", now=now).model_copy(
        update={"region": ProductRegion.UsEast}
    )
    repository.add_worker(worker, now=now)
    request = _request("regional-request", now=now).model_copy(
        update={"region": ProductRegion.EuCentral}
    )
    repository.enqueue_container_request(request, ready_at=now)
    [claim] = repository.claim_ready_container_requests(now=now)

    with pytest.raises(SchedulerRepositoryError, match="outside the selected region"):
        repository.dispatch_claimed_container_request(worker.worker_id, claim, now=now)

    stored = repository.get_worker(worker.worker_id)
    assert stored is not None
    assert stored.free_cpu_millicores == worker.free_cpu_millicores
    assert stored.free_memory_mib == worker.free_memory_mib
    assert repository.acknowledge_container_request(claim)


def test_pinned_zone_dispatch_requires_matching_worker_without_consuming_a_failed_claim(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 9, 9, tzinfo=UTC)
    repository = RedisSchedulerWorkerRepository(real_redis_actors.client())
    worker = _available_worker("zonal-worker", now=now).model_copy(
        update={"availability_zone": "use1-az1"}
    )
    repository.add_worker(worker, now=now)
    request = _request("zonal-request", now=now).model_copy(
        update={"availability_zone": "use1-az5"}
    )
    repository.enqueue_container_request(request, ready_at=now)
    [claim] = repository.claim_ready_container_requests(now=now)

    with pytest.raises(SchedulerRepositoryError, match="outside the selected availability zone"):
        repository.dispatch_claimed_container_request(worker.worker_id, claim, now=now)

    stored = repository.get_worker(worker.worker_id)
    assert stored is not None
    assert stored.free_cpu_millicores == worker.free_cpu_millicores
    assert stored.free_memory_mib == worker.free_memory_mib
    repository.add_worker(worker.model_copy(update={"availability_zone": "use1-az5"}), now=now)
    dispatched = repository.dispatch_claimed_container_request(worker.worker_id, claim, now=now)
    assert dispatched.free_cpu_millicores == worker.free_cpu_millicores - request.cpu_millicores


def test_real_redis_claims_are_unique_and_expired_leases_recover(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repositories = [
        RedisSchedulerWorkerRepository(real_redis_actors.client()) for _index in range(8)
    ]
    assert len({repository.redis.transport_identity for repository in repositories}) == 8
    for index in range(32):
        assert (
            repositories[0].enqueue_container_request(
                _request(f"claim-{index}", now=now),
                ready_at=now,
            )
            == 1
        )

    barrier = Barrier(len(repositories))

    def claim(repository: RedisSchedulerWorkerRepository) -> list[SchedulerContainerRequestClaim]:
        barrier.wait()
        return repository.claim_ready_container_requests(now=now, limit=32, lease_seconds=10)

    with ThreadPoolExecutor(max_workers=len(repositories)) as executor:
        batches = list(executor.map(claim, repositories))
    claims = [item for batch in batches for item in batch]
    claimed_ids = [item.request.container_id for item in claims]
    assert len(claimed_ids) == 32
    assert len(set(claimed_ids)) == 32
    assert all(repositories[0].acknowledge_container_request(item) for item in claims)

    recoverable = _request("lease-recovery", now=now)
    repositories[0].enqueue_container_request(recoverable, ready_at=now)
    first = repositories[0].claim_ready_container_requests(
        now=now,
        limit=1,
        lease_seconds=1,
    )[0]
    recovered = repositories[1].claim_ready_container_requests(
        now=now + timedelta(seconds=2),
        limit=1,
        lease_seconds=10,
    )[0]
    assert recovered.request.container_id == recoverable.container_id
    assert recovered.token != first.token
    assert not repositories[0].acknowledge_container_request(first)
    assert repositories[1].requeue_container_request(
        recovered,
        recovered.request.requeued(now=now + timedelta(seconds=3)),
        ready_at=now + timedelta(seconds=3),
    )
    final = repositories[2].claim_ready_container_requests(
        now=now + timedelta(seconds=3),
        limit=1,
    )[0]
    assert repositories[2].acknowledge_container_request(final)


@pytest.mark.anyio
async def test_real_redis_dispatch_and_cancellation_have_one_terminal_winner(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repositories = [
        RedisSchedulerWorkerRepository(real_redis_actors.client()) for _index in range(2)
    ]
    repositories[0].add_worker(_available_worker("worker-1", now=now), now=now)
    request = _request("dispatch-1", now=now)
    repositories[0].enqueue_container_request(request, ready_at=now)
    claim = repositories[0].claim_ready_container_requests(now=now, limit=1)[0]
    barrier = Barrier(2)

    def dispatch(repository: RedisSchedulerWorkerRepository) -> bool:
        barrier.wait()
        try:
            repository.dispatch_claimed_container_request("worker-1", claim, now=now)
        except SchedulerRepositoryError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        dispatched = list(executor.map(dispatch, repositories))
    assert dispatched.count(True) == 1
    assert dispatched.count(False) == 1
    assert (
        await repositories[0].wait_for_next_container_request(
            async_redis,
            "worker-1",
            timeout_seconds=0.01,
        )
        == request
    )
    # Delivery is at least once: a take nobody acknowledged is handed out again
    # rather than destroyed, and only the acknowledgement retires it.
    assert (
        await repositories[1].wait_for_next_container_request(
            async_redis,
            "worker-1",
            timeout_seconds=0.01,
        )
        == request
    )
    assert await repositories[0].acknowledge_worker_request(
        async_redis,
        "worker-1",
        request.container_id,
    )
    assert not repositories[1].has_recoverable_container_request(
        request.container_id,
        worker_id="worker-1",
    )

    cancellable = _request("cancel-1", now=now)
    await repositories[0].enqueue_worker_request(async_redis, "worker-1", cancellable)
    start = asyncio.Event()

    async def cancel() -> bool:
        await start.wait()
        return await asyncio.to_thread(
            repositories[0].cancel_worker_request,
            "worker-1",
            cancellable.container_id,
        )

    async def dequeue() -> SchedulerWorkerRequest | None:
        await start.wait()
        return await repositories[1].wait_for_next_container_request(
            async_redis,
            "worker-1",
            timeout_seconds=0.01,
        )

    cancelled = asyncio.create_task(cancel())
    dequeued = asyncio.create_task(dequeue())
    start.set()
    cancel_won, delivered = await asyncio.gather(cancelled, dequeued)
    # The cancellation reaches the request on whichever of the worker's two lists
    # it is on, so a request already handed out is still cancellable.
    assert cancel_won
    assert delivered is None or delivered.container_id == cancellable.container_id
    assert not repositories[0].has_recoverable_container_request(
        cancellable.container_id,
        worker_id="worker-1",
    )


@pytest.mark.anyio
async def test_real_redis_unacknowledged_take_survives_the_consumer(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repositories = [
        RedisSchedulerWorkerRepository(real_redis_actors.client()) for _index in range(2)
    ]
    request = _request("take-1", now=now)
    await repositories[0].enqueue_worker_request(async_redis, "worker-1", request)

    taken = await repositories[0].wait_for_next_container_request(
        async_redis,
        "worker-1",
        timeout_seconds=1.0,
    )

    assert taken == request
    assert repositories[0].has_recoverable_container_request(
        request.container_id,
        worker_id="worker-1",
    )
    assert (
        await repositories[1].wait_for_next_container_request(
            async_redis,
            "worker-1",
            timeout_seconds=1.0,
        )
        == request
    )
    assert await repositories[1].acknowledge_worker_request(
        async_redis,
        "worker-1",
        request.container_id,
    )
    assert not await repositories[0].acknowledge_worker_request(
        async_redis,
        "worker-1",
        request.container_id,
    )
    assert not repositories[0].has_recoverable_container_request(
        request.container_id,
        worker_id="worker-1",
    )


def test_real_redis_concurrency_reserve_and_release_are_bounded_and_idempotent(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repositories = [
        RedisSchedulerContainerRepository(real_redis_actors.client()) for _index in range(12)
    ]
    barrier = Barrier(len(repositories))

    def reserve(index: int) -> tuple[str, ConcurrencyReservationStatus, bool]:
        barrier.wait()
        container_id = f"reservation-{index}"
        decision = repositories[index].reserve_concurrency(
            workspace_id="workspace-1",
            container_id=container_id,
            workspace_gpu_quota=5,
            workspace_cpu_quota_millicores=500,
            request_gpu_count=1,
            request_cpu_millicores=100,
            now=now,
        )
        return container_id, decision.status, decision.changed

    with ThreadPoolExecutor(max_workers=len(repositories)) as executor:
        results = list(executor.map(reserve, range(len(repositories))))
    accepted = [
        container_id
        for container_id, status, _changed in results
        if status is ConcurrencyReservationStatus.Ok
    ]
    assert len(accepted) == 5
    assert all(
        changed
        for _container_id, status, changed in results
        if status is ConcurrencyReservationStatus.Ok
    )
    counter = repositories[0].get_concurrency_counter("workspace-1")
    assert (counter.gpu_count, counter.cpu_millicores) == (5, 500)

    releases = [
        repositories[index].release_concurrency_reservation(
            "workspace-1",
            container_id,
            now=now + timedelta(seconds=1),
        )
        for index, container_id in enumerate(accepted)
    ]
    assert all(decision.status is ConcurrencyReservationStatus.Ok for decision in releases)
    assert all(decision.changed for decision in releases)
    repeated = repositories[0].release_concurrency_reservation(
        "workspace-1",
        accepted[0],
        now=now + timedelta(seconds=2),
    )
    assert repeated.status is ConcurrencyReservationStatus.Missing
    assert not repeated.changed
    counter = repositories[0].get_concurrency_counter("workspace-1")
    assert (counter.gpu_count, counter.cpu_millicores) == (0, 0)
