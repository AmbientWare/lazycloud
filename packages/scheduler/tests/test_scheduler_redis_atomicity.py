from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Never, Protocol

import pytest
from coordination.redis_client import RedisClient
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
from tests.redis_fakes import FakeRedis


class RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


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
        pool="default",
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


def test_real_redis_dispatch_and_cancellation_have_one_terminal_winner(
    real_redis_actors: RealRedisActors,
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
    assert repositories[0].get_next_container_request("worker-1") == request
    assert repositories[1].get_next_container_request("worker-1") is None

    cancellable = _request("cancel-1", now=now)
    repositories[0].enqueue_worker_request("worker-1", cancellable)
    barrier = Barrier(2)

    def cancel() -> bool:
        barrier.wait()
        return repositories[0].cancel_worker_request("worker-1", cancellable.container_id)

    def dequeue() -> SchedulerWorkerRequest | None:
        barrier.wait()
        return repositories[1].get_next_container_request("worker-1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancelled = executor.submit(cancel)
        dequeued = executor.submit(dequeue)
        cancel_won = cancelled.result()
        delivered = dequeued.result()
    assert cancel_won is (delivered is None)
    assert delivered is None or delivered.container_id == cancellable.container_id
    assert repositories[0].get_next_container_request("worker-1") is None


def test_real_redis_blocking_take_delivers_payload_once(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repositories = [
        RedisSchedulerWorkerRepository(real_redis_actors.client()) for _index in range(2)
    ]
    request = _request("take-1", now=now)
    repositories[0].enqueue_worker_request("worker-1", request)
    barrier = Barrier(2)

    def take(repository: RedisSchedulerWorkerRepository) -> SchedulerWorkerRequest | None:
        barrier.wait()
        return repository.wait_for_next_container_request("worker-1", timeout_seconds=0.2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(take, repositories))
    assert results.count(request) == 1
    assert results.count(None) == 1


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
            gpu_limit=5,
            cpu_limit_millicores=500,
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
