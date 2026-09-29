from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Never

import pytest
from compute.state import RedisComputeStateRepository
from coordination.redis_client import AsyncRedisClient, RedisClient
from redis.exceptions import ResponseError
from scheduler.state import (
    ConcurrencyReservationStatus,
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    SchedulerContainerRequestClaim,
    SchedulerRepositoryError,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
    WorkerRequestCancellation,
)
from shared.placement import Placement, ProductRegion
from shared.routing import AgentBackendRoute, BackendRouteState
from shared.scheduling import SchedulerContainerState, SchedulerContainerStatus
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis


def test_route_readiness_commits_projections_and_fences_replacement_and_deletion(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute = RedisComputeStateRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    opening = AgentBackendRoute(
        route_id="route",
        workspace_id="workspace",
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        machine_id="machine",
        container_id="container",
        local_target="127.0.0.1:8001",
    )
    compute.save_agent_route_state(opening)
    containers.set_container_address("container", "route://route", route=opening)
    containers.set_worker_address("container", "route://route", route=opening)
    containers.set_container_address_map("container", {8001: "route://route"}, routes=[opening])
    ready = opening.model_copy(update={"state": BackendRouteState.Ready})
    assert containers.update_backend_route(opening, ready)
    address = containers.get_container_address("container")
    worker_address = containers.get_worker_address("container")
    assert address is not None and address.route == ready
    assert worker_address is not None and worker_address.route == ready
    assert containers.get_container_address_map("container").routes == [ready]
    assert (
        compute.get_agent_route_state(
            opening.workspace_id, opening.capacity_owner_id, opening.machine_id, opening.route_id
        )
        == ready
    )

    replacement = opening.model_copy(update={"local_target": "127.0.0.2:8001"})
    containers.set_container_address("container", "route://route", route=replacement)
    assert containers.update_backend_route(ready, ready)
    address = containers.get_container_address("container")
    assert address is not None and address.route == replacement
    compute.save_agent_route_state(replacement)
    assert not containers.update_backend_route(ready, ready)
    assert compute.delete_agent_route_state(
        workspace_id=opening.workspace_id,
        capacity_owner_id=opening.capacity_owner_id,
        machine_id=opening.machine_id,
        route_id=opening.route_id,
    )
    assert not containers.update_backend_route(replacement, ready)
    assert (
        compute.get_agent_route_state(
            opening.workspace_id, opening.capacity_owner_id, opening.machine_id, opening.route_id
        )
        is None
    )


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
        fairness_account_id="account-1",
        placement=Placement.platform(),
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
        placement=Placement.platform(),
        status=SchedulerWorkerStatus.Available,
        request_poll_expires_at=datetime.now(UTC) + timedelta(minutes=1),
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


def test_account_fair_claims_keep_large_work_and_other_accounts_progressing(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    repository = RedisSchedulerWorkerRepository(real_redis_actors.client())
    for index in range(20):
        repository.enqueue_container_request(
            _request(f"hot-{index:02}", now=now).model_copy(
                update={
                    "workspace_id": "hot-a" if index % 2 else "hot-b",
                    "fairness_account_id": "hot",
                    "cpu_millicores": 8000 if index == 0 else 100,
                }
            ),
            ready_at=now - timedelta(seconds=1),
        )
    repository.enqueue_container_request(
        _request("quiet", now=now).model_copy(
            update={"workspace_id": "quiet", "fairness_account_id": "quiet"}
        ),
        ready_at=now,
    )
    first = repository.claim_ready_container_requests(now=now, limit=1)
    second = repository.claim_ready_container_requests(now=now, limit=1)
    assert [claim.request.container_id for claim in first + second] == ["hot-00", "quiet"]
    assert all(repository.acknowledge_container_request(claim) for claim in first + second)
    remaining = repository.claim_ready_container_requests(now=now, limit=20)
    assert len(remaining) == 19
    assert len({claim.request.container_id for claim in remaining}) == 19


def test_retried_claim_does_not_charge_its_account_again(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime.now(UTC)
    repository = RedisSchedulerWorkerRepository(real_redis_actors.client())
    heavy = _request("heavy", now=now).model_copy(update={"cpu_millicores": 8000})
    repository.enqueue_container_request(heavy, ready_at=now - timedelta(seconds=1))
    for index in range(10):
        repository.enqueue_container_request(
            _request(f"other-{index:02}", now=now).model_copy(
                update={"fairness_account_id": "account-2"}
            ),
            ready_at=now,
        )
    [first] = repository.claim_ready_container_requests(now=now)
    assert first.request.container_id == "heavy"
    repository.requeue_container_request(first, heavy, ready_at=now - timedelta(seconds=1))
    for index in range(8):
        [other] = repository.claim_ready_container_requests(now=now)
        assert other.request.container_id == f"other-{index:02}"
        assert repository.acknowledge_container_request(other)
    [retry] = repository.claim_ready_container_requests(now=now)
    assert retry.request.container_id == "heavy"
    with pytest.raises(ResponseError, match="fairness account cannot change"):
        repository.requeue_container_request(
            retry, heavy.model_copy(update={"fairness_account_id": "account-2"}), ready_at=now
        )
    repository.requeue_container_request(retry, heavy, ready_at=now + timedelta(seconds=1))
    repository.enqueue_container_request(
        _request("new-small", now=now), ready_at=now - timedelta(seconds=1)
    )
    next_claims = repository.claim_ready_container_requests(now=now, limit=2)
    assert {claim.request.container_id for claim in next_claims} == {"new-small", "other-08"}


def test_continuously_ready_retries_yield_to_other_accounts_across_claim_batches(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime.now(UTC)
    repository = RedisSchedulerWorkerRepository(real_redis_actors.client())
    for container_id, account, cpu in (
        ("retry-a", "a", 1000),
        ("retry-c", "c", 1000),
        ("large-first", "b", 8000),
        ("large-next", "b", 8000),
    ):
        repository.enqueue_container_request(
            _request(container_id, now=now).model_copy(
                update={"fairness_account_id": account, "cpu_millicores": cpu}
            ),
            ready_at=now,
        )
    initial = repository.claim_ready_container_requests(now=now, limit=3)
    assert {claim.request.container_id for claim in initial} == {
        "retry-a",
        "retry-c",
        "large-first",
    }
    for claim in initial:
        if claim.request.container_id.startswith("retry-"):
            repository.requeue_container_request(claim, claim.request, ready_at=now)
        else:
            assert repository.acknowledge_container_request(claim)
    selected: list[str] = []
    for _ in range(3):
        [claim] = repository.claim_ready_container_requests(now=now, limit=1)
        selected.append(claim.request.container_id)
        repository.requeue_container_request(claim, claim.request, ready_at=now)
    assert selected == ["retry-a", "retry-c", "large-next"]


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

    async def cancel() -> WorkerRequestCancellation:
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
    assert cancel_won.removed
    assert delivered is None or cancel_won.delivered
    assert delivered is None or delivered.container_id == cancellable.container_id
    assert not repositories[0].has_recoverable_container_request(
        cancellable.container_id,
        worker_id="worker-1",
    )


def test_pending_recovery_and_running_transition_have_one_winner(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = RedisSchedulerContainerRepository(real_redis_actors.client())
    repository.set_container_state(
        SchedulerContainerState(
            container_id="starting",
            stub_id="stub-1",
            workspace_id="workspace-1",
            status=SchedulerContainerStatus.Pending,
        )
    )
    barrier = Barrier(2)

    def start() -> SchedulerContainerStatus:
        barrier.wait()
        return repository.update_container_status(
            "starting", SchedulerContainerStatus.Running
        ).next_status

    def recover() -> SchedulerContainerState | None:
        barrier.wait()
        return repository.cancel_container_request("starting", only_if_pending=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        started = executor.submit(start)
        recovered = executor.submit(recover)
        start_status = started.result()
        recovered_state = recovered.result()

    assert recovered_state is not None
    assert recovered_state.status is start_status
    assert repository.is_container_cancelled("starting") is (
        start_status is SchedulerContainerStatus.Stopping
    )
    repeated = repository.cancel_container_request("starting", only_if_pending=True)
    assert repeated is not None and repeated.status is start_status


@pytest.mark.anyio
async def test_dispatch_requires_a_live_request_poll_even_when_keepalive_continues(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    repository = RedisSchedulerWorkerRepository(redis)
    now = datetime.now(UTC)
    expired_at = now - timedelta(seconds=1)
    worker = _available_worker("worker-intake", now=now).model_copy(
        update={"request_poll_expires_at": expired_at}
    )
    repository.add_worker(worker, now=now)
    kept_alive = repository.set_keep_alive(worker.worker_id)
    assert kept_alive.request_intake_status(at=now) is SchedulerWorkerStatus.Unavailable
    request = _request("intake-request", now=now)
    repository.enqueue_container_request(request, ready_at=now)
    claim = repository.claim_ready_container_requests(now=now, limit=1)[0]

    with pytest.raises(SchedulerRepositoryError, match="polling lease expired"):
        repository.dispatch_claimed_container_request(
            worker.worker_id, claim, now=expired_at - timedelta(seconds=1)
        )
    assert repository.has_recoverable_container_request(request.container_id)
    fresh = await repository.record_worker_request_poll(async_redis, worker.worker_id)
    assert fresh.request_intake_status(at=now) is SchedulerWorkerStatus.Available
    repository.dispatch_claimed_container_request(worker.worker_id, claim, now=now)
    delivered = await repository.wait_for_next_container_request(
        async_redis, worker.worker_id, timeout_seconds=0.01
    )
    assert delivered is not None and delivered.container_id == request.container_id


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
