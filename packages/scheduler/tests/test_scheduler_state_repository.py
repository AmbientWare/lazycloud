from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from compute.agent_control import DEFAULT_PRIVATE_EXECUTOR, agent_machine_worker_id
from compute.state import ComputeAgentTokenState, RedisComputeStateRepository
from control.service import ControlPlaneService
from coordination.redis_client import AsyncRedisClient, RedisClient, RedisSettings, redis_text
from database.records.apps import StubRecord
from database.repositories.apps import DeploymentRepository
from database.repositories.orchestration import ContainerRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.functions.service import FunctionControlService
from execution.pods.service import PodControlService
from operations.management import ManagementService
from pydantic import JsonValue, TypeAdapter
from scheduler.agent_pool import AgentPoolConfig
from scheduler.autoscaling import AutoscalingDriver, PodAutoscaler
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.containers import (
    DEFAULT_SCHEDULER_REQUEUE_DELAY_SECONDS,
    SchedulerContainerAssignmentRecorder,
    SchedulerContainerDispatchResult,
    SchedulerContainerDispatchStatus,
    SchedulerContainerFailureHandler,
    SchedulerContainerLifecycleEvents,
    SchedulerContainerPlacement,
    SchedulerContainerRequestService,
    SchedulerContainerStateRepository,
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
    SchedulerContainerWorkerRepository,
)
from scheduler.fleet import (
    DEFAULT_MAX_SCHEDULE_RETRY_COUNT,
    SchedulerContainerStatus,
    SchedulerRetryReason,
    SchedulerWorkerStatus,
    WorkerPoolStateSnapshot,
)
from scheduler.pool_state import SchedulerPoolStateService
from scheduler.service import Scheduler, SchedulerStateStores, SchedulerWorkloadControls
from scheduler.state import (
    DEFAULT_CONTAINER_REQUEST_CLAIM_LEASE_SECONDS,
    AgentBackendRoute,
    CapacityReservationDispatchAllocation,
    ConcurrencyCounter,
    ConcurrencyReservation,
    ConcurrencyReservationStatus,
    ContainerRequestCancelledError,
    ContainerRequestClaimNotOwnedError,
    NetworkIpMutationAction,
    RedisOrphanedContainerConfirmationRepository,
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerNetworkIpRepository,
    RedisWorkerPoolStateRepository,
    SchedulerContainerRequestClaim,
    SchedulerContainerState,
    SchedulerRepositoryError,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    WorkerCapacityChange,
    WorkerRepositoryLockKind,
    WorkerReservedCapacity,
    capacity_memory_mib,
    plan_move_network_container_ip,
    plan_remove_network_container_ip,
    plan_worker_capacity_change,
    release_concurrency,
    reserve_concurrency,
)
from scheduler.workers import SchedulerWorkerAdminService
from shared.billing_quotes import ContainerShape
from shared.compute_policy import MachinePool
from shared.container_requests import OciRuntimeName, StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.cron import CronJobRecord
from shared.deployment_records import Deployment, DeploymentSpec, Resources
from shared.deployments import DeploymentKind, StubKind
from shared.errors import ConflictError, NotFoundError
from shared.http.functions import FunctionClaimRequest
from shared.image_building.authoring import ImageSpec
from shared.realtime.contracts import (
    CloudEventRecord,
    EventDataInput,
    EventRecordType,
    create_cloud_event_record,
)
from shared.scheduling import WorkerUnavailableReason
from shared.tasks import RetryPolicy, Task, TaskStatus, is_terminal_task_status
from shared.usage import UsageBillingOwner
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis

_EVENT_DATA_ADAPTER = TypeAdapter(dict[str, JsonValue | datetime])


def test_container_exit_code_retains_typed_termination_reason() -> None:
    redis = RedisClient(FakeRedis(), key_prefix="scheduler-exit-reason")
    repository = RedisSchedulerContainerRepository(redis)

    repository.set_exit_code(
        "container-preempted",
        562,
        termination_reason=StopContainerReason.Preempted,
    )

    assert repository.get_exit_code("container-preempted") == 562
    assert repository.get_termination_reason("container-preempted") is StopContainerReason.Preempted


class RecordingContainerScheduler:
    def __init__(self) -> None:
        self.requests: list[SchedulerWorkerRequest] = []
        self.ready_at: list[datetime | None] = []

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        self.requests.append(request)
        self.ready_at.append(ready_at)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
        )


class _UnownedWorkspaces:
    """No workspace here has an account behind it.

    Every worker these cases build is shared capacity, which placement decides
    without asking who owns the request.
    """

    def owner_user_id(self, workspace_id: str) -> str:
        _ = workspace_id
        return ""


class _RecordingDispatchWake:
    def __init__(self, *, wake_results: list[bool] | None = None) -> None:
        self.signal_count = 0
        self.waits: list[float] = []
        self.wake_results = list(wake_results or [])

    def signal(self) -> bool:
        self.signal_count += 1
        return True

    def wait(self, *, timeout_seconds: float) -> bool:
        self.waits.append(timeout_seconds)
        if self.wake_results:
            return self.wake_results.pop(0)
        raise KeyboardInterrupt


class _RecordingLifecycleEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str | EventRecordType, dict[str, JsonValue | datetime]]] = []

    def append_event(
        self,
        event_type: str | EventRecordType,
        data: EventDataInput,
        *,
        event_id: str | None = None,
    ) -> CloudEventRecord:
        event = create_cloud_event_record(
            event_type,
            data,
            event_id=event_id or "recorded-event",
        )
        source = data.model_dump(mode="python") if isinstance(data, ContractModel) else data
        self.events.append((event_type, _EVENT_DATA_ADAPTER.validate_python(source)))
        return event


class _RuntimeAssignmentRecorder:
    def __init__(self) -> None:
        self.assignments: list[tuple[str, str, str, str, str | None, str | None]] = []
        self.shapes: list[ContainerShape | None] = []
        self.cleared: list[tuple[str, str]] = []

    def assign_runtime(
        self,
        *,
        container_id: str,
        workspace_id: str,
        runtime_worker_id: str,
        runtime_machine_id: str,
        compute_worker_id: str | None = None,
        compute_machine_id: str | None = None,
        shape: ContainerShape | None = None,
    ) -> None:
        self.assignments.append(
            (
                container_id,
                workspace_id,
                runtime_worker_id,
                runtime_machine_id,
                compute_worker_id,
                compute_machine_id,
            )
        )
        self.shapes.append(shape)

    def clear_runtime_assignment(
        self,
        *,
        container_id: str,
        runtime_worker_id: str,
    ) -> None:
        self.cleared.append((container_id, runtime_worker_id))


class _IdentityPlacement:
    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        return request


class _DiscardFailureHandler:
    def mark_scheduling_failed(
        self,
        request: SchedulerWorkerRequest,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> None:
        del request, reason, now


def _request_service(
    workers: SchedulerContainerWorkerRepository,
    containers: SchedulerContainerStateRepository,
    *,
    placement: SchedulerContainerPlacement | None = None,
    failure_handler: SchedulerContainerFailureHandler | None = None,
    assignments: SchedulerContainerAssignmentRecorder | None = None,
    dispatch_wake: _RecordingDispatchWake | None = None,
    lifecycle_events: SchedulerContainerLifecycleEvents | None = None,
    capacity_reservations: CapacityReservationService | None = None,
    requeue_delay_seconds: float = DEFAULT_SCHEDULER_REQUEUE_DELAY_SECONDS,
    max_retry_count: int = DEFAULT_MAX_SCHEDULE_RETRY_COUNT,
    retry_grace_seconds: float = 180.0,
    claim_lease_seconds: float = DEFAULT_CONTAINER_REQUEST_CLAIM_LEASE_SECONDS,
) -> SchedulerContainerRequestService:
    return SchedulerContainerRequestService(
        workers,
        containers,
        placement=placement if placement is not None else _IdentityPlacement(),
        failure_handler=(
            failure_handler if failure_handler is not None else _DiscardFailureHandler()
        ),
        assignments=(assignments if assignments is not None else _RuntimeAssignmentRecorder()),
        dispatch_wake=(dispatch_wake if dispatch_wake is not None else _RecordingDispatchWake()),
        lifecycle_events=(
            lifecycle_events if lifecycle_events is not None else _RecordingLifecycleEvents()
        ),
        capacity_reservations=capacity_reservations,
        workspace_owners=_UnownedWorkspaces(),
        requeue_delay_seconds=requeue_delay_seconds,
        max_retry_count=max_retry_count,
        retry_grace_seconds=retry_grace_seconds,
        claim_lease_seconds=claim_lease_seconds,
    )


def _capacity_reservations(redis: RedisClient) -> CapacityReservationService:
    """Coordinate owner mutations the way the real scheduler process does.

    Final dispatch onto a worker that belongs to a capacity owner takes the owner
    mutation lock, so a service built without this coordinator refuses to
    dispatch at all.
    """
    return CapacityReservationService(RedisCapacityReservationRepository(redis), tuple)


@pytest.fixture
async def async_redis(
    real_redis_actors: RealRedisActors,
) -> AsyncIterator[AsyncRedisClient]:
    client = AsyncRedisClient.from_settings(
        RedisSettings(url=real_redis_actors.url, key_prefix=real_redis_actors.prefix)
    )
    try:
        yield client
    finally:
        await client.close()


async def _worker_delivery_empty(
    redis: AsyncRedisClient,
    repository: RedisSchedulerWorkerRepository,
    worker_id: str,
) -> bool:
    queued, inflight = await asyncio.gather(
        redis.list_length(repository.keys.worker_requests(worker_id)),
        redis.list_length(repository.keys.worker_inflight_requests(worker_id)),
    )
    return queued == 0 and inflight == 0


def _backlog_request(
    redis: RedisClient,
    repository: RedisSchedulerWorkerRepository,
) -> SchedulerWorkerRequest:
    request_id = redis_text(redis.sorted_set_range(repository.keys.container_requests(), 0, 0)[0])
    payload = redis.hash_get(repository.keys.container_request_payloads(), request_id)
    assert payload is not None
    return SchedulerWorkerRequest.model_validate_json(redis_text(payload))


def _claim_and_acknowledge_requests(
    repository: RedisSchedulerWorkerRepository,
    *,
    now: datetime,
    limit: int,
) -> list[SchedulerWorkerRequest]:
    claims = repository.claim_ready_container_requests(now=now, limit=limit)
    for claim in claims:
        assert repository.acknowledge_container_request(claim)
    return [claim.request for claim in claims]


def _assigned_request(
    redis: RedisClient,
    repository: RedisSchedulerWorkerRepository,
    worker_id: str,
) -> SchedulerWorkerRequest:
    request_id = redis.list_index(repository.keys.worker_requests(worker_id), 0)
    assert request_id is not None
    payload = redis.hash_get(
        repository.keys.worker_request_payloads(worker_id),
        redis_text(request_id),
    )
    assert payload is not None
    return SchedulerWorkerRequest.model_validate_json(redis_text(payload))


def _assert_redis_ttl(redis: RedisClient, key: str, expected_seconds: int) -> None:
    ttl = int(redis.ttl(key))
    assert expected_seconds - 1 <= ttl <= expected_seconds


def _create_cron_function(
    isolated_services: ApiServices,
) -> tuple[Deployment, StubRecord, CronJobRecord]:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="cron-fn",
            kind=DeploymentKind.Function,
            handler="module:func",
            image=ImageSpec(python_version="3.11"),
            cron="every 1m",
        )
    )
    stub = next(
        item
        for item in ControlPlaneService(isolated_services.context).list_stubs()
        if item.deployment_id == deployment.id
    )
    # Declaring the schedule is what creates it; there is no second call.
    schedules = isolated_services.cron_jobs.list()
    assert len(schedules) == 1
    cron_job = schedules[0]
    assert cron_job.cron == "*/1 * * * *"
    assert cron_job.next_run_at is not None
    return deployment, stub, cron_job


def _cron_scheduler(services: ApiServices, redis: RedisClient) -> Scheduler:
    return Scheduler(
        services,
        workloads=SchedulerWorkloadControls(functions=FunctionControlService(services)),
        states=SchedulerStateStores(cron_job_locks=redis),
    )


def test_cron_failure_retries_same_run_then_persists_terminal_failure(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    container_scheduler = RecordingContainerScheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=container_scheduler),
    )
    isolated_services.deployments.deploy(
        DeploymentSpec(
            name="retrying-cron",
            kind=DeploymentKind.Function,
            handler="module:func",
            cron="every 1m",
            retry_policy=RetryPolicy.from_retries(1),
        )
    )
    schedules = isolated_services.cron_jobs.list()
    assert len(schedules) == 1
    cron_job = schedules[0]
    assert cron_job.next_run_at is not None

    runs = _cron_scheduler(isolated_services, real_redis_actors.client()).tick(
        now=cron_job.next_run_at
    )
    assert len(runs) == 1
    assert runs[0].task_id is not None
    task_id = runs[0].task_id
    functions = FunctionControlService(isolated_services)

    initial = isolated_services.tasks.get(task_id)
    # A cron container is started for the stub like any other, so it takes its
    # run by claiming it rather than being addressed by it.
    assert initial.stub_id is not None
    assert (
        functions.function_claim(
            FunctionClaimRequest(
                stub_id=initial.stub_id,
                container_id=container_scheduler.requests[0].container_id,
            )
        ).task
        is not None
    )
    initial = isolated_services.tasks.get(task_id)
    assert initial.container_id is not None
    first = isolated_services.tasks.start(
        task_id,
        container_id=initial.container_id,
    )
    assert first.container_id is not None

    def finish_first_attempt(_index: int) -> Task:
        return functions.finish_function_task(
            first.id,
            TaskStatus.Failed,
            container_id=first.container_id or "",
            error="RuntimeError: first attempt",
            exit_code=1,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        finishes = list(executor.map(finish_first_attempt, range(32)))
    assert all(item.status is TaskStatus.Retry for item in finishes)
    retry = isolated_services.tasks.get(task_id)

    assert retry.id == task_id
    assert retry.status is TaskStatus.Retry
    assert retry.attempt_number == 1
    assert retry.max_attempts == 2
    assert retry.container_id is None
    assert len(container_scheduler.requests) == 1

    previous_container = isolated_services.containers.get(first.container_id)
    previous_container.status = ContainerStatus.Failed
    previous_container.finished_at = datetime.now(UTC)
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).records.upsert(
            previous_container,
            workspace_id=previous_container.workspace_id,
            name=previous_container.name,
            status=previous_container.status.value,
        )
    scheduled = functions.schedule_due_retries(now=datetime.now(UTC))
    assert len(scheduled) == 1
    assert len(container_scheduler.requests) == 2
    assert (
        container_scheduler.requests[0].container_id != container_scheduler.requests[1].container_id
    )
    task_events = isolated_services.events.list(
        resource_type="task",
        resource_id=task_id,
    )
    assert sum(event.action == "task.retry" for event in task_events) == 1
    assert sum(event.action == "task.retry.scheduled" for event in task_events) == 1
    for _ in range(100):
        assert functions.schedule_due_retries(now=datetime.now(UTC) + timedelta(days=1)) == []
    assert len(container_scheduler.requests) == 2

    retry_task = isolated_services.tasks.get(task_id)
    assert retry_task.stub_id is not None
    assert (
        functions.function_claim(
            FunctionClaimRequest(
                stub_id=retry_task.stub_id,
                container_id=container_scheduler.requests[1].container_id,
            )
        ).task
        is not None
    )
    retry_task = isolated_services.tasks.get(task_id)
    assert retry_task.container_id == container_scheduler.requests[1].container_id
    with pytest.raises(ConflictError, match="is assigned to container"):
        isolated_services.tasks.start(task_id, container_id=str(uuid4()))

    def start_retry_attempt(_index: int) -> Task:
        return isolated_services.tasks.start(
            task_id,
            container_id=retry_task.container_id,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        starts = list(executor.map(start_retry_attempt, range(32)))
    assert all(item.status is TaskStatus.Running for item in starts)
    assert len(isolated_services.tasks.attempts(task_id)) == 2

    second = isolated_services.tasks.get(task_id)
    failed = functions.finish_function_task(
        second.id,
        TaskStatus.Failed,
        error="RuntimeError: final attempt",
        exit_code=1,
    )

    assert failed.id == task_id
    assert failed.status is TaskStatus.Failed
    assert failed.attempt_number == 2
    assert failed.max_attempts == 2
    assert failed.error == "RuntimeError: final attempt"
    attempts = isolated_services.tasks.attempts(task_id)
    assert [attempt.status for attempt in attempts] == [TaskStatus.Retry, TaskStatus.Failed]


def test_stopped_cron_deployment_cancels_due_retry_and_never_revives_it(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    container_scheduler = RecordingContainerScheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=container_scheduler),
    )
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="pausable-cron",
            kind=DeploymentKind.Function,
            handler="module:func",
            cron="every 1m",
            retry_policy=RetryPolicy.from_retries(1, delay_seconds=10),
        )
    )
    schedules = isolated_services.cron_jobs.list()
    assert len(schedules) == 1
    cron_job = schedules[0]
    assert cron_job.next_run_at is not None

    runs = _cron_scheduler(isolated_services, real_redis_actors.client()).tick(
        now=cron_job.next_run_at
    )
    assert runs[0].task_id is not None
    task_id = runs[0].task_id
    functions = FunctionControlService(isolated_services)
    initial = isolated_services.tasks.get(task_id)
    # A cron container is started for the stub like any other, so it takes its
    # run by claiming it rather than being addressed by it.
    assert initial.stub_id is not None
    assert (
        functions.function_claim(
            FunctionClaimRequest(
                stub_id=initial.stub_id,
                container_id=container_scheduler.requests[0].container_id,
            )
        ).task
        is not None
    )
    initial = isolated_services.tasks.get(task_id)
    assert initial.container_id is not None
    started = isolated_services.tasks.start(task_id, container_id=initial.container_id)
    assert started.container_id is not None
    finished_container_id = started.container_id
    retry = functions.finish_function_task(
        task_id,
        TaskStatus.Failed,
        error="RuntimeError: retry later",
        exit_code=1,
    )
    assert retry.status is TaskStatus.Retry
    assert retry.next_retry_at is not None
    assert len(container_scheduler.requests) == 1
    assert retry.container_id is None
    finished_container = isolated_services.containers.get(finished_container_id)
    finished_container.status = ContainerStatus.Exited
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).records.upsert(
            finished_container,
            workspace_id=finished_container.workspace_id,
            name=finished_container.name,
            status=finished_container.status.value,
        )

    management = ManagementService(isolated_services)
    management.set_deployment_active("default", deployment.id, active=False)
    scheduled = functions.schedule_due_retries(now=retry.next_retry_at)

    assert scheduled == []
    cancelled = isolated_services.tasks.get(task_id)
    assert cancelled.status is TaskStatus.Cancelled
    assert cancelled.error == "scheduled deployment is inactive"
    assert len(container_scheduler.requests) == 1
    assert isolated_services.cron_jobs.list()[0].enabled is False

    management.set_deployment_active("default", deployment.id, active=True)
    assert isolated_services.cron_jobs.list()[0].enabled is True
    assert functions.schedule_due_retries(now=retry.next_retry_at + timedelta(days=1)) == []
    assert len(container_scheduler.requests) == 1

    management.delete_deployment("default", deployment.id)
    assert isolated_services.cron_jobs.list() == []


def test_scheduler_tick_skips_cron_function_when_lock_is_held(
    isolated_services: ApiServices,
) -> None:
    container_scheduler = RecordingContainerScheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=container_scheduler),
    )
    _, stub, cron_job = _create_cron_function(isolated_services)
    fake = FakeRedis()
    redis = RedisClient(fake, key_prefix="test")
    fake.set(redis.key(f"function:cron_jobs_lock:{stub.id}"), "held")

    assert cron_job.next_run_at is not None
    runs = _cron_scheduler(isolated_services, redis).tick(now=cron_job.next_run_at)

    assert len(runs) == 1
    assert not runs[0].enqueued
    assert runs[0].task_id is None
    assert runs[0].message_id is None
    assert runs[0].reason == "cron job lock not acquired"
    assert isolated_services.collections.queue_depth("tasks") == 0
    assert isolated_services.tasks.list() == []
    assert container_scheduler.requests == []


def test_new_cron_version_takes_over_the_prior_schedule(
    isolated_services: ApiServices,
) -> None:
    """One schedule fires per resource, however many versions it has had.

    Held by the row's key rather than by a sweep: the schedule is named for the
    deployment's subdomain, which every version of a resource shares, so the
    newest deploy upserts the same row and the previous version stops firing
    without anything having to go looking for it.
    """

    first, _first_stub, first_job = _create_cron_function(isolated_services)
    second = isolated_services.deployments.deploy(
        DeploymentSpec(
            name=first.name,
            kind=DeploymentKind.Function,
            handler="module:func_v2",
            cron="0 * * * *",
        )
    )
    second_stub = next(
        item
        for item in ControlPlaneService(isolated_services.context).list_stubs()
        if item.deployment_id == second.id
    )

    jobs = isolated_services.cron_jobs.list()
    assert len(jobs) == 1
    assert jobs[0].name == first_job.name
    assert jobs[0].deployment_id == second.id
    assert jobs[0].cron == "0 * * * *"
    assert second.stub_id == second_stub.id


def test_inactive_cron_deployment_never_enqueues(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    deployment, _stub, cron_job = _create_cron_function(isolated_services)
    deployment.active = False
    workspace_id = ControlPlaneService(isolated_services.context).get_workspace("default").id
    with isolated_services.context.database.session() as session:
        DeploymentRepository(session).records.upsert(
            deployment,
            workspace_id=workspace_id,
            name=deployment.name,
            status="inactive",
        )
    assert cron_job.next_run_at is not None

    runs = _cron_scheduler(isolated_services, real_redis_actors.client()).tick(
        now=cron_job.next_run_at
    )

    assert len(runs) == 1
    assert not runs[0].enqueued
    assert runs[0].reason == "deployment inactive"


def test_scheduler_worker_repository_requeues_removed_worker_requests(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repo = RedisSchedulerWorkerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    worker = repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1000,
            free_gpu_count=1,
            total_cpu_millicores=1000,
            total_memory_mib=1000,
            total_gpu_count=1,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    assert worker.resource_version == 0
    _assert_redis_ttl(redis, repo.keys.worker_state("worker-1"), 900)

    request_payload: dict[str, JsonValue] = {
        "startup": {"entrypoint": ["python3.12", "-m", "runner.function"]},
        "arguments": [1, "two", True, None, {"nested": 3.5}],
    }
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=500,
        memory_mib=100,
        gpu=["T4"],
        gpu_count=1,
        pool_selector="aws",
        payload=request_payload,
        timestamp=now,
    )
    scheduled = repo.schedule_container_request("worker-1", request, now=now)
    assert scheduled.free_cpu_millicores == 500
    assert scheduled.free_memory_mib == 875
    assert scheduled.free_gpu_count == 0
    updated_worker = repo.get_worker("worker-1")
    assert updated_worker is not None
    assert updated_worker.resource_version == 1

    removed = repo.remove_worker(
        "worker-1",
        now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    )
    assert removed.removed
    assert removed.request_ids == ["container-1"]
    assert repo.get_worker("worker-1") is None
    assert not redis.set_contains(repo.keys.worker_index(), repo.keys.worker_state("worker-1"))
    assert set(redis.sorted_set_range(repo.keys.container_requests(), 0, -1)) == {
        request.container_id
    }
    requeued = _backlog_request(redis, repo)
    assert requeued.retry_count == 1
    assert requeued.container_id == "container-1"
    assert requeued.pool_selector == "aws"
    assert requeued.payload == request_payload

    add_plan = plan_worker_capacity_change(
        scheduled,
        request,
        WorkerCapacityChange.Add,
    )
    assert add_plan.accepted
    assert add_plan.worker.free_memory_mib == 1000
    assert capacity_memory_mib(100) == 125


@pytest.mark.parametrize("entrypoint", ["cleanup", "list"])
@pytest.mark.anyio
async def test_scheduler_worker_repository_requeues_expired_worker_requests(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
    entrypoint: str,
) -> None:
    redis = real_redis_actors.client()
    repo = RedisSchedulerWorkerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1000,
            total_cpu_millicores=1000,
            total_memory_mib=1000,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=250,
        memory_mib=100,
        timestamp=now,
    )
    await repo.enqueue_worker_request(async_redis, "worker-1", request)
    redis.delete(repo.keys.worker_state("worker-1"))

    if entrypoint == "cleanup":
        [cleanup] = repo.cleanup_missing_workers(now=now + timedelta(seconds=5))
        assert cleanup.worker_id == "worker-1"
        assert cleanup.request_ids == ["container-1"]
    else:
        assert repo.list_workers() == []

    assert not redis.set_contains(
        repo.keys.worker_index(),
        repo.keys.worker_state("worker-1"),
    )
    assert redis.list_length(repo.keys.worker_requests("worker-1")) == 0
    requeued = _backlog_request(redis, repo)
    assert requeued.container_id == "container-1"
    assert requeued.retry_count == 1


@pytest.mark.anyio
async def test_expired_worker_requeues_delivered_requests_but_not_ones_it_acted_on(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    """A gone worker's in-flight requests come back, except the ones it already ran.

    Reclaim is bound to the worker's keepalive rather than a clock of its own,
    and a delivered request whose container has left `pending` was acted on:
    requeueing it would start a second container for one durable row.

    A container that has already finished counts exactly as much as one still
    running. A worker whose acknowledgements fail long enough gives up on them
    while the work runs to completion, so the request stays recorded in flight
    and its container reaches a terminal status — the state that reads as
    "never started" if only `running` is checked, and runs the work twice.
    """

    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1000,
            total_cpu_millicores=1000,
            total_memory_mib=1000,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    delivered = [
        SchedulerWorkerRequest(
            workspace_id="ws-1",
            stub_id="stub-1",
            container_id=container_id,
            timestamp=now,
        )
        for container_id in ("container-1", "container-2")
    ]
    for request in delivered:
        await workers.enqueue_worker_request(async_redis, "worker-1", request)
        containers.set_container_state(
            SchedulerContainerState(
                container_id=request.container_id,
                stub_id=request.stub_id,
                workspace_id=request.workspace_id,
                worker_id="worker-1",
                status=SchedulerContainerStatus.Pending,
            )
        )
        assert (
            await workers.wait_for_next_container_request(
                async_redis,
                "worker-1",
                timeout_seconds=0.01,
            )
            == request
        )
        assert await workers.acknowledge_worker_request(
            async_redis,
            "worker-1",
            request.container_id,
        )
    # Both are delivered again; only the first is handed out, and its container
    # runs to completion while the acknowledgement never lands.
    for request in delivered:
        await workers.enqueue_worker_request(async_redis, "worker-1", request)
    assert (
        await workers.wait_for_next_container_request(
            async_redis,
            "worker-1",
            timeout_seconds=0.01,
        )
        == delivered[0]
    )
    containers.update_container_status(
        "container-1",
        SchedulerContainerStatus.Complete,
    )

    redis.delete(workers.keys.worker_state("worker-1"))
    [cleanup] = workers.cleanup_missing_workers(now=now + timedelta(seconds=5))

    assert cleanup.request_ids == ["container-2"]
    assert redis.list_length(workers.keys.worker_inflight_requests("worker-1")) == 0
    assert redis.hash_length(workers.keys.worker_request_payloads("worker-1")) == 0
    requeued = _backlog_request(redis, workers)
    assert requeued.container_id == "container-2"
    assert requeued.retry_count == 1


def test_worker_rollout_slots_bound_parallel_drains_and_fence_release(
    real_redis_actors: RealRedisActors,
) -> None:
    repo = RedisSchedulerWorkerRepository(real_redis_actors.client())
    owner_id = "11111111-1111-4111-8111-111111111111"
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert repo.claim_worker_rollout_slot(
        owner_id,
        "worker-1",
        "revision-a",
        max_unavailable=2,
        now=now,
    )
    assert repo.claim_worker_rollout_slot(
        owner_id,
        "worker-2",
        "revision-a",
        max_unavailable=2,
        now=now,
    )
    assert not repo.claim_worker_rollout_slot(
        owner_id,
        "worker-3",
        "revision-a",
        max_unavailable=2,
        now=now,
    )
    assert not repo.release_worker_rollout_slot(owner_id, "worker-1", "revision-b")
    assert repo.release_worker_rollout_slot(owner_id, "worker-1", "revision-a")
    assert repo.claim_worker_rollout_slot(
        owner_id,
        "worker-3",
        "revision-a",
        max_unavailable=2,
        now=now,
    )


@pytest.mark.anyio
async def test_scheduler_worker_repository_lifecycle_capacity_queue_and_image_pull_locks(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    repo = RedisSchedulerWorkerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Pending,
            free_cpu_millicores=1000,
            free_memory_mib=1000,
            free_gpu_count=1,
            total_cpu_millicores=1000,
            total_memory_mib=1000,
            total_gpu_count=1,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )

    pending = repo.set_keep_alive("worker-1")
    assert pending.status is SchedulerWorkerStatus.Pending
    available = repo.toggle_worker_available("worker-1")
    assert available.status is SchedulerWorkerStatus.Available
    _assert_redis_ttl(redis, repo.keys.worker_state("worker-1"), 60)

    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=250,
        memory_mib=100,
        gpu=["T4"],
        gpu_count=1,
        timestamp=now,
    )
    removed_capacity = repo.update_worker_capacity(
        "worker-1",
        request,
        WorkerCapacityChange.Remove,
    )
    assert removed_capacity.worker.free_cpu_millicores == 750
    assert removed_capacity.worker.free_memory_mib == 875
    assert removed_capacity.worker.free_gpu_count == 0

    restored_capacity = repo.update_worker_capacity(
        "worker-1",
        request,
        WorkerCapacityChange.Add,
    )
    assert restored_capacity.worker.free_cpu_millicores == 1000
    assert restored_capacity.worker.free_memory_mib == 1000
    assert restored_capacity.worker.free_gpu_count == 1

    assert repo.add_container_to_worker("worker-1", "container-1") == 1
    assert repo.remove_container_from_worker("worker-1", "container-1") == 1

    await repo.enqueue_worker_request(async_redis, "worker-1", request)
    assert (
        await repo.wait_for_next_container_request(
            async_redis,
            "worker-1",
            timeout_seconds=0.01,
        )
        == request
    )
    assert await repo.acknowledge_worker_request(
        async_redis,
        "worker-1",
        request.container_id,
    )
    assert await _worker_delivery_empty(async_redis, repo, "worker-1")

    repo.enqueue_container_request(request, ready_at=now)
    assert _claim_and_acknowledge_requests(repo, now=now, limit=10) == [request]
    future = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    repo.enqueue_container_request(request, ready_at=future)
    assert _claim_and_acknowledge_requests(repo, now=now, limit=10) == []
    assert _claim_and_acknowledge_requests(repo, now=future, limit=10) == [request]

    disabled = repo.disable_worker("worker-1", reason=WorkerUnavailableReason.OperatorCordon)
    assert disabled.status is SchedulerWorkerStatus.Unavailable

    image_lock = repo.set_image_pull_lock(
        "worker-1",
        "image-1",
        ttl_seconds=12,
        retries=0,
    )
    assert image_lock.kind is WorkerRepositoryLockKind.ImagePull
    assert image_lock.acquired
    _assert_redis_ttl(redis, repo.keys.image_pull_lock("worker-1", "image-1"), 12)
    blocked = repo.set_image_pull_lock("worker-1", "image-1", ttl_seconds=12, retries=0)
    assert not blocked.acquired
    wrong_release = repo.remove_image_pull_lock("worker-1", "image-1", "wrong")
    assert not wrong_release.released
    assert wrong_release.reason == "lock token mismatch"
    released = repo.remove_image_pull_lock("worker-1", "image-1", image_lock.token)
    assert released.released


def test_scheduler_request_claim_recovers_after_process_loss(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repository = RedisSchedulerWorkerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        timestamp=now,
    )
    assert repository.enqueue_container_request(request, ready_at=now) == 1

    first_claims = repository.claim_ready_container_requests(
        now=now,
        limit=1,
        lease_seconds=5,
    )

    assert [claim.request for claim in first_claims] == [request]
    assert repository.has_recoverable_container_request(request.container_id)
    assert (
        repository.claim_ready_container_requests(
            now=now + timedelta(seconds=4),
            limit=1,
            lease_seconds=5,
        )
        == []
    )

    recovered_claims = repository.claim_ready_container_requests(
        now=now + timedelta(seconds=6),
        limit=1,
        lease_seconds=5,
    )

    assert [claim.request for claim in recovered_claims] == [request]
    assert not repository.acknowledge_container_request(first_claims[0])
    assert repository.acknowledge_container_request(recovered_claims[0])
    assert not repository.has_recoverable_container_request(request.container_id)
    assert redis.sorted_set_cardinality(repository.keys.container_requests()) == 0
    assert redis.sorted_set_cardinality(repository.keys.container_request_claims()) == 0
    assert redis.hash_length(repository.keys.container_request_payloads()) == 0
    assert redis.hash_length(repository.keys.container_request_claim_owners()) == 0


@pytest.mark.anyio
async def test_claim_dispatch_commit_survives_scheduler_crash_without_duplicate_delivery(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=250,
        memory_mib=128,
        timestamp=now,
    )
    assert workers.enqueue_container_request(request, ready_at=now) == 1
    [claim] = workers.claim_ready_container_requests(
        now=now,
        limit=1,
        lease_seconds=5,
    )

    committed = workers.dispatch_claimed_container_request(
        "worker-1",
        claim,
        now=now,
    )

    assert committed.free_cpu_millicores == 750
    assert committed.resource_version == 1
    assert (
        workers.claim_ready_container_requests(
            now=now + timedelta(seconds=6),
            limit=1,
            lease_seconds=5,
        )
        == []
    )
    assert redis.list_range(workers.keys.worker_requests("worker-1"), 0, -1) == [
        request.container_id
    ]
    with pytest.raises(ContainerRequestClaimNotOwnedError):
        workers.dispatch_claimed_container_request("worker-1", claim, now=now)
    current = workers.get_worker("worker-1")
    assert current is not None
    assert current.free_cpu_millicores == 750
    assert current.resource_version == 1
    assert redis.list_range(workers.keys.worker_requests("worker-1"), 0, -1) == [
        request.container_id
    ]
    assert (
        await workers.wait_for_next_container_request(
            async_redis,
            "worker-1",
            timeout_seconds=0.01,
        )
        == request
    )
    assert await workers.acknowledge_worker_request(
        async_redis,
        "worker-1",
        request.container_id,
    )
    assert await _worker_delivery_empty(async_redis, workers, "worker-1")

    cancelled_request = request.model_copy(update={"container_id": "container-2"})
    assert workers.enqueue_container_request(cancelled_request, ready_at=now) == 1
    [cancelled_claim] = workers.claim_ready_container_requests(
        now=now,
        limit=1,
        lease_seconds=5,
    )
    assert containers.cancel_container_request(cancelled_request.container_id) is None
    with pytest.raises(ContainerRequestCancelledError):
        workers.dispatch_claimed_container_request("worker-1", cancelled_claim, now=now)
    after_cancellation = workers.get_worker("worker-1")
    assert after_cancellation is not None
    assert after_cancellation.free_cpu_millicores == 750
    assert after_cancellation.resource_version == 1
    assert redis.list_length(workers.keys.worker_requests("worker-1")) == 0
    assert not workers.has_recoverable_container_request(cancelled_request.container_id)


def test_scheduler_worker_admin_service_lists_cordons_drains_and_removes_workers(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    stopped: list[str] = []
    service = SchedulerWorkerAdminService(workers, containers, stop_container=stopped.append)

    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            machine_id="machine-1",
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id="pending-ctr",
            workspace_id="ws-1",
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Pending,
            scheduled_at=now,
        )
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id="running-ctr",
            workspace_id="ws-1",
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
            scheduled_at=now,
            started_at=now,
        )
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id="complete-ctr",
            workspace_id="ws-1",
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Complete,
            scheduled_at=now,
            started_at=now,
        )
    )

    listed = service.list_workers()

    assert [worker.id for worker in listed] == ["worker-1"]
    assert listed[0].status == "available"
    assert [container.container_id for container in listed[0].active_containers] == [
        "pending-ctr",
        "running-ctr",
    ]

    cordoned = service.cordon_worker("worker-1")
    assert cordoned.status == "disabled"
    uncordoned = service.uncordon_worker("worker-1")
    assert uncordoned.status == "available"

    def fail_stop(container_id: str) -> None:
        raise RuntimeError(f"cannot stop {container_id}")

    failing_service = SchedulerWorkerAdminService(
        workers,
        containers,
        stop_container=fail_stop,
    )
    with pytest.raises(ConflictError, match="cannot stop pending-ctr"):
        failing_service.drain_worker("worker-1")

    drained = service.drain_worker("worker-1")

    assert drained.worker.status == "disabled"
    assert drained.stopped_container_ids == ["pending-ctr", "running-ctr"]
    assert stopped == ["pending-ctr", "running-ctr"]

    removed = service.delete_worker("worker-1", now=now)
    assert removed.removed
    assert workers.get_worker("worker-1") is None
    with pytest.raises(NotFoundError, match="worker not found"):
        service.cordon_worker("worker-1")


def test_scheduler_container_repository_state_indexes_and_concurrency_release(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repo = RedisSchedulerContainerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    state = SchedulerContainerState(
        container_id="container-1",
        stub_id="stub-1",
        workspace_id="ws-1",
        worker_id="worker-1",
        scheduled_at=now,
        gpu_count=1,
        cpu_millicores=250,
        memory_mib=512,
    )
    repo.set_container_state(state)
    assert repo.list_by_stub("stub-1") == [state]
    assert repo.list_by_workspace("ws-1") == [state]
    assert repo.list_by_worker("worker-1") == [state]

    _seed_hash(
        redis,
        repo.keys.workspace_concurrency_counter("ws-1"),
        ConcurrencyCounter(
            workspace_id="ws-1",
            initialized=True,
            gpu_count=0,
            cpu_millicores=0,
            updated_at=now,
        ),
    )
    reserved = repo.reserve_concurrency(
        workspace_id="ws-1",
        container_id="container-1",
        workspace_gpu_quota=1,
        workspace_cpu_quota_millicores=500,
        request_gpu_count=1,
        request_cpu_millicores=250,
        now=now,
    )
    assert reserved.status is ConcurrencyReservationStatus.Ok
    assert redis.set_members(repo.keys.workspace_concurrency_reservation_index("ws-1")) == {
        "container-1"
    }

    running = repo.update_container_status(
        "container-1",
        SchedulerContainerStatus.Running,
        now=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
    )
    assert running.started_at_set
    running_state = repo.get_container_state("container-1")
    assert running_state is not None
    assert running_state.started_at is not None

    stopping = repo.update_container_status("container-1", SchedulerContainerStatus.Stopping)
    assert stopping.release_concurrency
    assert (
        repo.get_concurrency_counter("ws-1").gpu_count,
        repo.get_concurrency_counter("ws-1").cpu_millicores,
    ) == (0, 0)
    assert repo.get_concurrency_reservation("ws-1", "container-1") is None

    ignored = repo.update_container_status("container-1", SchedulerContainerStatus.Running)
    assert not ignored.changed
    assert ignored.next_status is SchedulerContainerStatus.Stopping
    _assert_redis_ttl(redis, repo.keys.container_cancellation("container-1"), 86_400)

    ignored_complete = repo.update_container_status(
        "container-1", SchedulerContainerStatus.Complete
    )
    assert not ignored_complete.changed
    assert ignored_complete.next_status is SchedulerContainerStatus.Stopping
    replacement = repo.set_container_state(
        state.model_copy(
            update={
                "worker_id": "worker-2",
                "status": SchedulerContainerStatus.Pending,
            }
        )
    )
    assert replacement.status is SchedulerContainerStatus.Stopping
    assert replacement.worker_id == "worker-1"

    repo.set_exit_code(
        "container-1",
        137,
        termination_reason=StopContainerReason.Preempted,
    )
    assert repo.get_exit_code("container-1") == 137
    assert repo.get_termination_reason("container-1") is StopContainerReason.Preempted
    _assert_redis_ttl(redis, repo.keys.container_exit_code("container-1"), 86_400)
    _assert_redis_ttl(redis, repo.keys.container_termination_reason("container-1"), 86_400)

    route = AgentBackendRoute(
        route_id="route-1",
        workspace_id="ws-1",
        pool=MachinePool("default"),
        machine_id="machine-1",
        worker_id="worker-1",
        port=8080,
        local_target="127.0.0.1:8080",
    )
    address = repo.set_container_address(
        "container-1",
        "10.0.0.10:8080",
        route=route,
    )
    assert address.route is not None
    assert address.route.container_id == "container-1"
    assert repo.get_container_address("container-1") == address

    address_map = repo.set_container_address_map(
        "container-1",
        {8080: "10.0.0.10:8080", 9090: "10.0.0.10:9090"},
        routes=[route],
    )
    assert repo.get_container_address_map("container-1") == address_map
    assert repo.get_container_address_map("missing").address_map == {}

    worker_address = repo.set_worker_address(
        "container-1",
        "worker-1.internal:9000",
        route=route,
    )
    assert repo.get_worker_address("container-1") == worker_address

    assert repo.delete_container_state("container-1")
    assert repo.get_container_address("container-1") is None
    assert repo.get_container_address_map("container-1").address_map == {}
    assert repo.get_worker_address("container-1") is None


@pytest.mark.anyio
async def test_scheduler_container_request_service_queues_selects_and_dispatches(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    assignments = _RuntimeAssignmentRecorder()
    dispatch_wake = _RecordingDispatchWake()
    lifecycle_events = _RecordingLifecycleEvents()
    service = _request_service(
        worker_repo,
        container_repo,
        assignments=assignments,
        dispatch_wake=dispatch_wake,
        lifecycle_events=lifecycle_events,
        capacity_reservations=_capacity_reservations(redis),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)

    worker_repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            machine_id="machine-1",
            pool=MachinePool("gpu-pool"),
            status=SchedulerWorkerStatus.Available,
            gpu_type="T4",
            runtime_class=OciRuntimeName.Runsc.value,
            runtime_classes=[OciRuntimeName.Runsc.value],
            free_cpu_millicores=1000,
            free_memory_mib=1000,
            free_gpu_count=1,
            total_cpu_millicores=1000,
            total_memory_mib=1000,
            total_gpu_count=1,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    worker_repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-2",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Unavailable,
            free_cpu_millicores=4000,
            free_memory_mib=4096,
            total_cpu_millicores=4000,
            total_memory_mib=4096,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=500,
        memory_mib=100,
        gpu=["T4"],
        gpu_count=1,
        pool_selector="gpu-pool",
        runtime_class="runsc",
        docker_enabled=True,
        timestamp=now,
        payload={
            "image_id": "image-workload-1",
            "build_id": "must-not-project",
            "archive_upload_capability": "must-not-project",
        },
    )

    submitted = service.submit(request, ready_at=now)
    assert submitted.status is SchedulerContainerSubmitStatus.Queued
    assert dispatch_wake.signal_count == 1
    state = container_repo.get_container_state("container-1")
    assert state is not None
    assert state.container_id == "container-1"
    assert state.stub_id == "stub-1"
    assert state.workspace_id == "ws-1"
    assert state.status is SchedulerContainerStatus.Pending
    assert state.cpu_millicores == 500
    assert state.memory_mib == 100
    assert state.image_id == "image-workload-1"
    assert state.image_build_id == ""
    assert state.image_build_upload_capability == ""

    dispatched = service.dispatch_ready(now=now, limit=10)

    assert len(dispatched) == 1
    assert dispatched[0].status is SchedulerContainerDispatchStatus.Dispatched
    assert dispatched[0].worker_id == "worker-1"
    queued = await worker_repo.wait_for_next_container_request(
        async_redis,
        "worker-1",
        timeout_seconds=0.01,
    )
    assert queued is not None
    assert queued.container_id == request.container_id
    assert queued.workspace_id == request.workspace_id
    assert queued.stub_id == request.stub_id
    assert queued.pool_selector == "gpu-pool"
    assert queued.runtime_class == "runsc"
    assert queued.docker_enabled
    assert await _worker_delivery_empty(async_redis, worker_repo, "worker-2")
    assigned_worker = worker_repo.get_worker("worker-1")
    assert assigned_worker is not None
    assert assigned_worker.free_cpu_millicores == 500
    assert assigned_worker.free_gpu_count == 0
    assigned = container_repo.get_container_state("container-1")
    assert assigned is not None
    assert assigned.worker_id == "worker-1"
    assert assigned.image_id == "image-workload-1"
    assert assigned.image_build_id == ""
    assert assigned.image_build_upload_capability == ""
    assert assignments.assignments == [("container-1", "ws-1", "worker-1", "machine-1", None, None)]
    assert assignments.cleared == []
    assert len(lifecycle_events.events) == 1
    event_type, lifecycle = lifecycle_events.events[0]
    assert event_type is EventRecordType.ContainerLifecycle
    assert lifecycle["event_id"] == "scheduler"
    assert lifecycle["container_id"] == "container-1"
    assert lifecycle["start_time"] == now
    assert lifecycle["end_time"] == now
    assert lifecycle["duration_ms"] == 0


def test_scheduler_dispatch_clears_runtime_assignment_when_queueing_fails(
    monkeypatch: pytest.MonkeyPatch,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    assignments = _RuntimeAssignmentRecorder()
    service = _request_service(
        workers,
        containers,
        assignments=assignments,
        capacity_reservations=_capacity_reservations(redis),
        requeue_delay_seconds=0,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            machine_id="machine-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
        )
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=100,
        memory_mib=128,
        timestamp=now,
    )
    assert service.submit(request, ready_at=now).accepted

    def fail_queue(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("worker queue unavailable")

    monkeypatch.setattr(
        RedisSchedulerWorkerRepository,
        "dispatch_claimed_container_request",
        fail_queue,
    )

    [result] = service.dispatch_ready(now=now, limit=1)

    assert result.status is SchedulerContainerDispatchStatus.Error
    assert assignments.assignments == [("container-1", "ws-1", "worker-1", "machine-1", None, None)]
    assert assignments.cleared == [("container-1", "worker-1")]
    pending = containers.get_container_state("container-1")
    assert pending is not None
    assert pending.status is SchedulerContainerStatus.Pending


@pytest.mark.anyio
async def test_scheduler_claim_dispatch_honors_cancellation_before_atomic_commit(
    monkeypatch: pytest.MonkeyPatch,
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    assignments = _RuntimeAssignmentRecorder()
    service = _request_service(
        workers,
        containers,
        assignments=assignments,
        capacity_reservations=_capacity_reservations(redis),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            machine_id="machine-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
        )
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=100,
        memory_mib=128,
        timestamp=now,
    )
    assert service.submit(request, ready_at=now).accepted
    dispatch = RedisSchedulerWorkerRepository.dispatch_claimed_container_request

    def cancel_before_commit(
        repository: RedisSchedulerWorkerRepository,
        worker_id: str,
        claim: SchedulerContainerRequestClaim,
        *,
        reserved_capacity: WorkerReservedCapacity | None = None,
        capacity_allocation: CapacityReservationDispatchAllocation | None = None,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord:
        containers.cancel_container_request(claim.request.container_id)
        return dispatch(
            repository,
            worker_id,
            claim,
            reserved_capacity=reserved_capacity,
            capacity_allocation=capacity_allocation,
            now=now,
        )

    monkeypatch.setattr(
        RedisSchedulerWorkerRepository,
        "dispatch_claimed_container_request",
        cancel_before_commit,
    )

    [result] = service.dispatch_ready(now=now, limit=1)

    assert result.status is SchedulerContainerDispatchStatus.Cancelled
    assert assignments.assignments == [("container-1", "ws-1", "worker-1", "machine-1", None, None)]
    assert assignments.cleared == [("container-1", "worker-1")]
    assert containers.get_container_state(request.container_id) is None
    assert containers.is_container_cancelled(request.container_id)
    assert await _worker_delivery_empty(async_redis, workers, "worker-1")
    worker = workers.get_worker("worker-1")
    assert worker is not None
    assert worker.free_cpu_millicores == 1000
    assert worker.resource_version == 0


@pytest.mark.anyio
async def test_worker_request_dequeue_holds_one_delivery_without_the_worker_mutation_lock(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    requests = [
        SchedulerWorkerRequest(
            workspace_id="ws-1",
            stub_id="stub-1",
            container_id=f"container-{index}",
        )
        for index in (1, 2)
    ]
    for request in requests:
        await workers.enqueue_worker_request(async_redis, "worker-1", request)

    worker_lock_key = workers.keys.worker_lock("worker-1")
    assert redis.set(worker_lock_key, "scheduler-owner", ex=10, nx=True)
    dequeued = await asyncio.gather(
        *(
            workers.wait_for_next_container_request(
                async_redis,
                "worker-1",
                timeout_seconds=0.01,
            )
            for _index in range(2)
        )
    )

    # A worker holds one delivery at a time: until it acknowledges the first
    # request, every take hands that same one back rather than moving on.
    assert {request.container_id for request in dequeued if request is not None} == {"container-1"}
    assert all(request is not None for request in dequeued)
    assert await workers.acknowledge_worker_request(
        async_redis,
        "worker-1",
        "container-1",
    )
    second = await workers.wait_for_next_container_request(
        async_redis,
        "worker-1",
        timeout_seconds=0.01,
    )
    assert second is not None
    assert second.container_id == "container-2"
    assert await workers.acknowledge_worker_request(
        async_redis,
        "worker-1",
        "container-2",
    )
    assert await _worker_delivery_empty(async_redis, workers, "worker-1")
    assert redis.get(worker_lock_key) == "scheduler-owner"


@pytest.mark.anyio
async def test_worker_request_blocking_pop_wakes_on_assignment_without_duplicate(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
    )

    waiting = asyncio.create_task(
        workers.wait_for_next_container_request(
            async_redis,
            "worker-1",
            timeout_seconds=1.0,
        )
    )
    await asyncio.sleep(0)
    await workers.enqueue_worker_request(async_redis, "worker-1", request)
    dequeued = await waiting

    assert dequeued == request
    assert await workers.acknowledge_worker_request(
        async_redis,
        "worker-1",
        request.container_id,
    )
    assert await _worker_delivery_empty(async_redis, workers, "worker-1")
    assert redis.hash_length(workers.keys.worker_request_payloads("worker-1")) == 0
    assert redis.list_length(workers.keys.worker_inflight_requests("worker-1")) == 0
    assert await _worker_delivery_empty(async_redis, workers, "worker-1")


@pytest.mark.anyio
async def test_scheduler_dispatch_records_the_placement_an_image_build_is_priced_from(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    assignments = _RuntimeAssignmentRecorder()
    service = _request_service(
        workers,
        containers,
        assignments=assignments,
        capacity_reservations=_capacity_reservations(redis),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            machine_id="machine-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            billing_owner=UsageBillingOwner.PlatformFleet,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
        )
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="image-build",
        container_id="22222222-2222-4222-8222-222222222222",
        cpu_millicores=100,
        memory_mib=128,
        timestamp=now,
    )
    assert service.submit(request, ready_at=now).accepted

    [result] = service.dispatch_ready(now=now, limit=1)

    assert result.status is SchedulerContainerDispatchStatus.Dispatched
    queued = await workers.wait_for_next_container_request(
        async_redis,
        "worker-1",
        timeout_seconds=0.01,
    )
    assert queued is not None
    assert queued.container_id == request.container_id
    assert assignments.assignments == [
        (request.container_id, "ws-1", "worker-1", "machine-1", None, None)
    ]
    assert assignments.shapes == [
        ContainerShape(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="",
            cpu_millicores=100,
            # The reservation, not the request: capacity is held with the
            # headroom the scheduler adds, and that is what is paid for.
            memory_mib=160,
            gpu_count=0,
        )
    ]
    assert assignments.cleared == []


@pytest.mark.anyio
async def test_scheduler_container_cancellation_cannot_be_dispatched_or_requeued(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    service = _request_service(
        worker_repo,
        container_repo,
        capacity_reservations=_capacity_reservations(redis),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    worker_repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1000,
            total_cpu_millicores=1000,
            total_memory_mib=1000,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=500,
        memory_mib=100,
        timestamp=now,
    )
    assert service.submit(request, ready_at=now).accepted
    container_repo.update_container_status(
        request.container_id,
        SchedulerContainerStatus.Stopping,
    )

    cancelled = service.dispatch_ready(now=now, limit=10)

    assert cancelled == []
    assert await _worker_delivery_empty(async_redis, worker_repo, "worker-1")
    assert redis.sorted_set_cardinality(worker_repo.keys.container_requests()) == 0
    assert redis.hash_length(worker_repo.keys.container_request_payloads()) == 0
    worker = worker_repo.get_worker("worker-1")
    assert worker is not None
    assert worker.free_cpu_millicores == 1000

    assigned_request = request.model_copy(update={"container_id": "container-2"})
    assert service.submit(assigned_request, ready_at=now).accepted
    assert service.dispatch_ready(now=now, limit=10)[0].dispatched
    container_repo.update_container_status(
        assigned_request.container_id,
        SchedulerContainerStatus.Stopping,
    )
    removed = worker_repo.remove_worker("worker-1", now=now)

    assert removed.requeued_count == 0
    assert removed.request_ids == []
    assert _claim_and_acknowledge_requests(worker_repo, now=now, limit=10) == []


def test_scheduler_cancellation_removes_only_owned_backlog_and_preserves_fence(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    service = _request_service(workers, containers)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    cancelled_request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-cancelled",
        timestamp=now,
    )
    retained_request = cancelled_request.model_copy(update={"container_id": "container-retained"})

    assert service.submit(cancelled_request, ready_at=now).accepted
    assert service.submit(retained_request, ready_at=now).accepted

    result = service.cancel(cancelled_request.container_id)
    assert result.state_found
    assert not result.worker_stop_required
    assert containers.delete_container_state(cancelled_request.container_id)
    assert workers.enqueue_container_request(cancelled_request, ready_at=now) == 0
    assert _claim_and_acknowledge_requests(workers, now=now, limit=10) == [retained_request]
    assert redis.exists(containers.keys.container_cancellation(cancelled_request.container_id))
    assert redis.sorted_set_cardinality(workers.keys.container_requests()) == 0
    assert redis.hash_length(workers.keys.container_request_payloads()) == 0


def test_workspace_container_state_cleanup_purges_only_owned_terminal_keys(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = RedisSchedulerContainerRepository(real_redis_actors.client())
    owned = SchedulerContainerState(
        container_id="container-owned",
        workspace_id="workspace-owned",
        stub_id="stub-owned",
    )
    unrelated = SchedulerContainerState(
        container_id="container-unrelated",
        workspace_id="workspace-unrelated",
        stub_id="stub-unrelated",
    )
    repository.set_container_state(owned)
    repository.set_container_state(unrelated)
    repository.update_container_status(
        owned.container_id,
        SchedulerContainerStatus.Stopping,
    )
    repository.update_container_status(
        unrelated.container_id,
        SchedulerContainerStatus.Stopping,
    )
    repository.set_exit_code(owned.container_id, 0)
    repository.set_exit_code(unrelated.container_id, 0)

    deleted = repository.delete_workspace_container_state(owned.workspace_id)

    assert deleted == 1
    assert repository.get_container_state(owned.container_id) is None
    assert not repository.is_container_cancelled(owned.container_id)
    assert repository.get_exit_code(owned.container_id) is None
    assert repository.get_container_state(unrelated.container_id) is not None
    assert repository.is_container_cancelled(unrelated.container_id)
    assert repository.get_exit_code(unrelated.container_id) == 0


def test_workspace_container_state_cleanup_purges_terminal_keys_after_state_deletion(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repository = RedisSchedulerContainerRepository(redis)
    container_id = "container-completed"
    repository.set_exit_code(container_id, 0)
    redis.set(repository.keys.container_cancellation(container_id), "1")

    deleted = repository.delete_workspace_container_state(
        "workspace-owned",
        container_ids=[container_id],
    )

    assert deleted == 0
    assert not repository.is_container_cancelled(container_id)
    assert repository.get_exit_code(container_id) is None
    assert repository.get_termination_reason(container_id) is None


def test_workspace_cleanup_discovers_ephemeral_container_after_state_deletion(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repository = RedisSchedulerContainerRepository(redis)
    owned = SchedulerContainerState(
        container_id="build-owned",
        workspace_id="workspace-owned",
        stub_id="image-build",
    )
    peer = SchedulerContainerState(
        container_id="build-peer",
        workspace_id="workspace-peer",
        stub_id="image-build",
    )
    repository.set_container_state(owned)
    repository.set_container_state(peer)
    repository.update_container_status(owned.container_id, SchedulerContainerStatus.Stopping)
    repository.update_container_status(peer.container_id, SchedulerContainerStatus.Stopping)
    assert repository.delete_container_state(owned.container_id)
    assert repository.delete_container_state(peer.container_id)

    ownership_index = repository.keys.container_workspace_ownership_index(owned.workspace_id)
    assert redis.set_members(ownership_index) == {owned.container_id}
    _assert_redis_ttl(redis, ownership_index, 86_400)
    assert repository.is_container_cancelled(owned.container_id)

    deleted = repository.delete_workspace_container_state(owned.workspace_id)

    assert deleted == 0
    assert not repository.is_container_cancelled(owned.container_id)
    assert redis.exists(ownership_index) == 0
    assert repository.is_container_cancelled(peer.container_id)
    assert redis.set_members(
        repository.keys.container_workspace_ownership_index(peer.workspace_id)
    ) == {peer.container_id}


@pytest.mark.anyio
async def test_scheduler_cancellation_removes_assigned_request_and_all_indexes(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    service = _request_service(
        workers,
        containers,
        capacity_reservations=_capacity_reservations(redis),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=500,
        memory_mib=128,
        timestamp=now,
    )
    assert service.submit(request, ready_at=now).accepted
    dispatched = service.dispatch_ready(now=now, limit=1)
    assert dispatched[0].status is SchedulerContainerDispatchStatus.Dispatched

    result = service.cancel(request.container_id)

    assert result.pending_request_removed
    assert not result.worker_stop_required
    assert containers.get_container_state(request.container_id) is None
    assert await _worker_delivery_empty(async_redis, workers, "worker-1")
    worker = workers.get_worker("worker-1")
    assert worker is not None
    assert worker.free_cpu_millicores == 1000
    assert worker.free_memory_mib == 1024
    state_key = containers.keys.container_state(request.container_id)
    assert not redis.set_contains(containers.keys.container_stub_index("stub-1"), state_key)
    assert not redis.set_contains(containers.keys.container_workspace_index("ws-1"), state_key)
    assert not redis.set_contains(containers.keys.container_worker_index("worker-1"), state_key)


def test_scheduler_stopping_transition_is_atomic_with_dispatch_state_replacement(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    container_repo = RedisSchedulerContainerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def assign(barrier: Barrier, assigned: SchedulerContainerState) -> None:
        barrier.wait()
        container_repo.set_container_state(assigned)

    def stop(barrier: Barrier, container_id: str) -> None:
        barrier.wait()
        container_repo.update_container_status(
            container_id,
            SchedulerContainerStatus.Stopping,
        )

    for index in range(20):
        container_id = f"container-{index}"
        pending = SchedulerContainerState(
            container_id=container_id,
            workspace_id="ws-1",
            stub_id="stub-1",
            status=SchedulerContainerStatus.Pending,
            scheduled_at=now,
        )
        assigned = pending.model_copy(update={"worker_id": "worker-1"})
        container_repo.set_container_state(pending)
        barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            assignments = [
                executor.submit(assign, barrier, assigned),
                executor.submit(stop, barrier, container_id),
            ]
            for assignment in assignments:
                assignment.result()

        state = container_repo.get_container_state(container_id)
        assert state is not None
        assert state.status is SchedulerContainerStatus.Stopping


@pytest.mark.anyio
async def test_scheduler_run_once_dispatches_when_pool_state_refresh_fails(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    service = _request_service(
        worker_repo,
        container_repo,
        capacity_reservations=_capacity_reservations(redis),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    worker_repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1000,
            total_cpu_millicores=1000,
            total_memory_mib=1000,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=500,
        memory_mib=100,
        timestamp=now,
    )
    assert service.submit(request, ready_at=now).accepted
    scheduler = Scheduler(
        workloads=SchedulerWorkloadControls(containers=service),
        states=SchedulerStateStores(pools=_failing_pool_state_service()),
        reconcile_agent_pools_enabled=False,
    )

    result = scheduler.run_once(
        now=now,
        include_cron_jobs=False,
        include_containers=True,
        container_limit=10,
    )

    assert len(result.container_dispatches) == 1
    assert result.container_dispatches[0].status is SchedulerContainerDispatchStatus.Dispatched
    assert result.pool_states == {}
    queued = await worker_repo.wait_for_next_container_request(
        async_redis,
        "worker-1",
        timeout_seconds=0.01,
    )
    assert queued is not None
    assert queued.container_id == "container-1"


@pytest.mark.anyio
async def test_scheduler_dispatch_resumes_an_expired_claim_after_restart(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    service = _request_service(
        worker_repo,
        container_repo,
        capacity_reservations=_capacity_reservations(redis),
        claim_lease_seconds=5,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    worker_repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1000,
            total_cpu_millicores=1000,
            total_memory_mib=1000,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=250,
        memory_mib=128,
        timestamp=now,
    )
    assert service.submit(request, ready_at=now).accepted
    abandoned = worker_repo.claim_ready_container_requests(
        now=now,
        limit=1,
        lease_seconds=5,
    )
    assert [claim.request for claim in abandoned] == [request]

    before_expiry = service.dispatch_ready(now=now + timedelta(seconds=4), limit=1)
    recovered = service.dispatch_ready(now=now + timedelta(seconds=6), limit=1)

    assert before_expiry == []
    assert len(recovered) == 1
    assert recovered[0].status is SchedulerContainerDispatchStatus.Dispatched
    assert await worker_repo.wait_for_next_container_request(
        async_redis,
        "worker-1",
        timeout_seconds=0.01,
    ) == request.model_copy(update={"timestamp": now + timedelta(seconds=6)})
    assert not worker_repo.acknowledge_container_request(abandoned[0])


def test_scheduler_reconciles_confirmed_unrecoverable_sql_container(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    network_repo = RedisWorkerNetworkIpRepository(redis)
    request_service = _request_service(
        worker_repo,
        container_repo,
        failure_handler=ContainerSchedulingPersistenceService(
            isolated_services.context,
            isolated_services.events,
            isolated_services.workspace_changes,
        ),
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        orphaned = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="orphaned-container",
                image="",
                command=[],
                workspace_id=workspace_id,
                status=ContainerStatus.Pending,
            )
        )
        recoverable = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="recoverable-container",
                image="",
                command=[],
                workspace_id=workspace_id,
                status=ContainerStatus.Pending,
            )
        )
    recoverable_request = SchedulerWorkerRequest(
        workspace_id=workspace_id,
        stub_id="container",
        container_id=recoverable.id,
    )
    assert request_service.submit(recoverable_request).accepted
    container_repo.set_worker_address(orphaned.id, "10.0.0.1:9000")
    network_repo.set_container_ip("test-network", orphaned.id, "10.10.0.8")
    scheduler = Scheduler(
        services=isolated_services,
        workloads=SchedulerWorkloadControls(containers=request_service),
        states=SchedulerStateStores(
            orphaned_container_networks=network_repo,
            orphaned_container_confirmations=RedisOrphanedContainerConfirmationRepository(redis),
        ),
        orphaned_container_reconcile_interval_seconds=0,
        orphaned_container_confirmation_seconds=60,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)

    first = scheduler.reconcile_orphaned_containers(now=now)
    confirmed = scheduler.reconcile_orphaned_containers(now=now + timedelta(seconds=61))

    assert first == []
    assert confirmed == [orphaned.id]
    assert isolated_services.containers.get(orphaned.id).status is ContainerStatus.Failed
    assert isolated_services.containers.get(recoverable.id).status is ContainerStatus.Pending
    assert container_repo.get_worker_address(orphaned.id) is None
    assert network_repo.get_container_ip("test-network", orphaned.id) is None


def test_scheduler_orphan_reconciliation_restores_pod_desired_capacity(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    request_service = _request_service(
        worker_repo,
        container_repo,
        failure_handler=ContainerSchedulingPersistenceService(
            isolated_services.context,
            isolated_services.events,
            isolated_services.workspace_changes,
        ),
    )
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="orphan-recovery-pod",
            kind=DeploymentKind.Pod,
            resources=Resources(keep_warm=-1),
            command=["python", "-m", "http.server"],
            ports={"8080": 8080},
        )
    )
    control = ControlPlaneService(isolated_services.context)
    stub = next(item for item in control.list_stubs() if item.deployment_id == deployment.id)
    stub = control.update_stub_config(
        stub.id,
        fields={"image": {"image_id": "img-pod"}},
    ).stub
    with isolated_services.context.database.session() as session:
        orphaned = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="orphaned-pod",
                image="img-pod",
                command=["python", "-m", "http.server"],
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                status=ContainerStatus.Pending,
            )
        )
    scheduler = Scheduler(
        services=isolated_services,
        workloads=SchedulerWorkloadControls(containers=request_service),
        states=SchedulerStateStores(
            orphaned_container_confirmations=RedisOrphanedContainerConfirmationRepository(redis),
        ),
        orphaned_container_reconcile_interval_seconds=0,
        orphaned_container_confirmation_seconds=60,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler.reconcile_orphaned_containers(now=now)

    reconciled = scheduler.reconcile_orphaned_containers(now=now + timedelta(seconds=61))

    assert reconciled == [orphaned.id]
    assert isolated_services.containers.get(orphaned.id).status is ContainerStatus.Failed

    recording_scheduler = RecordingContainerScheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=recording_scheduler),
    )
    result = AutoscalingDriver(
        isolated_services,
        redis=redis,
        workload=PodAutoscaler(
            isolated_services,
            redis=redis,
            pods=PodControlService(isolated_services, redis=redis),
        ),
        container_states=container_repo,
        container_requests=RedisSchedulerWorkerRepository(redis),
    ).reconcile(now=now + timedelta(seconds=62))[0]

    assert result.current_containers == 0
    assert result.desired_containers == 1
    assert [action.action for action in result.actions] == ["start"]
    assert len(recording_scheduler.requests) == 1
    assert recording_scheduler.requests[0].stub_id == stub.id
    assert recording_scheduler.requests[0].container_id != orphaned.id


def test_scheduler_ready_pop_and_worker_dispatch_are_atomic_under_parallel_schedulers(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    peer_redis = real_redis_actors.client()
    services = [
        _request_service(
            worker_repo,
            container_repo,
            capacity_reservations=_capacity_reservations(redis),
        ),
        _request_service(
            RedisSchedulerWorkerRepository(peer_redis),
            RedisSchedulerContainerRepository(peer_redis),
            capacity_reservations=_capacity_reservations(peer_redis),
        ),
    ]
    now = datetime(2026, 1, 1, tzinfo=UTC)
    worker_repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=1000,
        memory_mib=128,
        timestamp=now,
    )
    assert services[0].submit(request, ready_at=now).accepted

    def dispatch(index: int) -> list[SchedulerContainerDispatchResult]:
        return services[index].dispatch_ready(now=now, limit=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(dispatch, [0, 1]))

    flat = [item for batch in results for item in batch]
    dispatched = [
        item for item in flat if item.status is SchedulerContainerDispatchStatus.Dispatched
    ]
    assert len(dispatched) == 1
    assert dispatched[0].container_id == "container-1"
    assert redis.list_length(worker_repo.keys.worker_requests("worker-1")) == 1
    assert _claim_and_acknowledge_requests(worker_repo, now=now, limit=10) == []
    assigned_worker = worker_repo.get_worker("worker-1")
    assert assigned_worker is not None
    assert assigned_worker.free_cpu_millicores == 0


def test_worker_capacity_reservation_and_enqueue_are_worker_lock_guarded(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repo = RedisSchedulerWorkerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    requests = [
        SchedulerWorkerRequest(
            workspace_id="ws-1",
            stub_id="stub-1",
            container_id=f"container-{index}",
            cpu_millicores=1000,
            memory_mib=128,
            timestamp=now,
        )
        for index in (1, 2)
    ]

    def submit_request(request: SchedulerWorkerRequest) -> str:
        try:
            repo.schedule_container_request("worker-1", request, now=now)
        except SchedulerRepositoryError as exc:
            return str(exc)
        return "scheduled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit_request, requests))

    assert outcomes.count("scheduled") == 1
    assert redis.list_length(repo.keys.worker_requests("worker-1")) == 1
    queued_request = _assigned_request(redis, repo, "worker-1")
    assert queued_request.container_id in {"container-1", "container-2"}
    worker = repo.get_worker("worker-1")
    assert worker is not None
    assert worker.free_cpu_millicores == 0
    assert worker.free_memory_mib == 864


def test_scheduler_container_request_service_bounds_no_capacity_retries(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    failure_handler = _FailureHandler()
    service = _request_service(
        worker_repo,
        container_repo,
        failure_handler=failure_handler,
        requeue_delay_seconds=2,
        max_retry_count=1,
        # The count, not the grace, is what this proves.
        retry_grace_seconds=0.0,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=500,
        memory_mib=100,
        timestamp=now,
        payload={"image_id": "image-workload-retry"},
    )

    submitted = service.submit(request, ready_at=now)
    assert submitted.status is SchedulerContainerSubmitStatus.Queued
    pending = container_repo.get_container_state(request.container_id)
    assert pending is not None
    assert pending.image_id == "image-workload-retry"
    waiting = service.dispatch_ready(now=now, limit=10)

    assert len(waiting) == 1
    assert waiting[0].status is SchedulerContainerDispatchStatus.Waiting
    assert waiting[0].reason == SchedulerRetryReason.ScheduleFailed.value
    requeued = _backlog_request(redis, worker_repo)
    assert requeued.retry_count == 1
    assert requeued.timestamp == now
    assert requeued.payload["image_id"] == "image-workload-retry"
    retrying = container_repo.get_container_state(request.container_id)
    assert retrying is not None
    assert retrying.status is SchedulerContainerStatus.Pending
    assert retrying.image_id == "image-workload-retry"

    failed = service.dispatch_ready(now=now + timedelta(seconds=2), limit=10)

    assert len(failed) == 1
    assert failed[0].status is SchedulerContainerDispatchStatus.Failed
    assert failed[0].reason.startswith(SchedulerRetryReason.RetryLimit.value)
    state = container_repo.get_container_state("container-1")
    assert state is not None
    assert state.status is SchedulerContainerStatus.Failed
    assert state.image_id == "image-workload-retry"
    assert state.failure_reason.startswith(SchedulerRetryReason.RetryLimit.value)
    [(failed_container_id, failed_reason)] = failure_handler.calls
    assert failed_container_id == "container-1"
    assert failed_reason.startswith(SchedulerRetryReason.RetryLimit.value)


def test_scheduler_image_build_failure_persists_coordination_evidence_and_fails_the_container(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    failure_handler = _FailureHandler()
    service = _request_service(
        worker_repo,
        container_repo,
        failure_handler=failure_handler,
        max_retry_count=0,
        retry_grace_seconds=0.0,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="image-build",
        container_id="33333333-3333-4333-8333-333333333333",
        cpu_millicores=500,
        memory_mib=100,
        timestamp=now,
        payload={
            "kind": "image-build",
            "build_id": "build-1",
            "image_id": "image-1",
            "archive_upload_capability": "a" * 32,
        },
    )

    assert service.submit(request, ready_at=now).accepted
    pending = container_repo.get_container_state(request.container_id)
    assert pending is not None
    assert pending.image_build_id == "build-1"
    assert pending.image_id == "image-1"
    assert pending.image_build_upload_capability == "a" * 32
    [failed] = service.dispatch_ready(now=now, limit=1)

    assert failed.status is SchedulerContainerDispatchStatus.Failed
    assert failed.reason.startswith(SchedulerRetryReason.RetryLimit.value)
    state = container_repo.get_container_state(request.container_id)
    assert state is not None
    assert state.status is SchedulerContainerStatus.Failed
    assert state.image_build_id == "build-1"
    assert state.image_id == "image-1"
    assert state.image_build_upload_capability == "a" * 32
    assert state.failure_reason.startswith(SchedulerRetryReason.RetryLimit.value)
    [(failed_container_id, failed_reason)] = failure_handler.calls
    assert failed_container_id == request.container_id
    assert failed_reason.startswith(SchedulerRetryReason.RetryLimit.value)


def test_scheduler_container_request_service_waits_for_pending_worker_without_retry_count(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    service = _request_service(worker_repo, container_repo)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    worker_repo.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Pending,
            free_cpu_millicores=1000,
            free_memory_mib=1000,
            total_cpu_millicores=1000,
            total_memory_mib=1000,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=500,
        memory_mib=100,
        retry_count=3,
        timestamp=now,
    )

    submitted = service.submit(request, ready_at=now)
    assert submitted.status is SchedulerContainerSubmitStatus.Queued
    waiting = service.dispatch_ready(now=now, limit=10)

    assert len(waiting) == 1
    assert waiting[0].status is SchedulerContainerDispatchStatus.Waiting
    assert waiting[0].worker_id == "worker-1"
    assert waiting[0].reason == SchedulerRetryReason.WorkerCapacityWait.value
    requeued = _backlog_request(redis, worker_repo)
    assert requeued.retry_count == 3
    assert requeued.timestamp == now


def test_scheduler_container_request_service_reserves_quota_on_submit(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    service = _request_service(worker_repo, container_repo)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_hash(
        redis,
        container_repo.keys.workspace_concurrency_counter("ws-1"),
        ConcurrencyCounter(
            workspace_id="ws-1",
            initialized=True,
            gpu_count=0,
            cpu_millicores=0,
            updated_at=now,
        ),
    )
    request = SchedulerWorkerRequest(
        workspace_id="ws-1",
        stub_id="stub-1",
        container_id="container-1",
        cpu_millicores=500,
        memory_mib=100,
        gpu=["T4"],
        gpu_count=1,
        workspace_gpu_quota=1,
        workspace_cpu_quota_millicores=1000,
        timestamp=now,
    )

    submitted = service.submit(request, ready_at=now)

    assert submitted.status is SchedulerContainerSubmitStatus.Queued
    counter = container_repo.get_concurrency_counter("ws-1")
    assert counter.gpu_count == 1
    assert counter.cpu_millicores == 500
    reservation = container_repo.get_concurrency_reservation("ws-1", "container-1")
    assert reservation is not None
    assert reservation.gpu_count == 1
    assert reservation.cpu_millicores == 500


def test_scheduler_container_repository_repairs_concurrency_counter_from_active_state(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repo = RedisSchedulerContainerRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repo.set_container_state(
        SchedulerContainerState(
            container_id="container-active",
            stub_id="stub-1",
            workspace_id="ws-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
            scheduled_at=now,
            gpu_count=1,
            cpu_millicores=500,
            memory_mib=128,
        )
    )

    repaired = repo.reserve_concurrency(
        workspace_id="ws-1",
        container_id="container-new",
        workspace_gpu_quota=2,
        workspace_cpu_quota_millicores=1500,
        request_gpu_count=1,
        request_cpu_millicores=500,
        now=now,
    )

    assert repaired.status is ConcurrencyReservationStatus.Ok
    counter = repo.get_concurrency_counter("ws-1")
    assert counter.initialized
    assert not counter.repairing
    assert (counter.gpu_count, counter.cpu_millicores) == (2, 1000)
    active_reservation = repo.get_concurrency_reservation("ws-1", "container-active")
    new_reservation = repo.get_concurrency_reservation("ws-1", "container-new")
    assert active_reservation is not None
    assert new_reservation is not None

    _seed_hash(
        redis,
        repo.keys.workspace_concurrency_counter("ws-1"),
        ConcurrencyCounter(
            workspace_id="ws-1",
            initialized=True,
            gpu_count=99,
            cpu_millicores=99_000,
            updated_at=now - timedelta(seconds=60),
            repaired_at=now - timedelta(seconds=60),
        ),
    )
    repaired_after_throttle = repo.reserve_concurrency(
        workspace_id="ws-1",
        container_id="container-third",
        workspace_gpu_quota=3,
        workspace_cpu_quota_millicores=2000,
        request_gpu_count=1,
        request_cpu_millicores=500,
        now=now,
    )

    assert repaired_after_throttle.status is ConcurrencyReservationStatus.Ok
    counter = repo.get_concurrency_counter("ws-1")
    assert (counter.gpu_count, counter.cpu_millicores) == (3, 1500)


def test_concurrency_reservation_decisions_cover_repair_and_limits() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    counter = ConcurrencyCounter(workspace_id="ws-1", gpu_count=1, cpu_millicores=500)
    existing = ConcurrencyReservation(
        workspace_id="ws-1",
        container_id="container-1",
        gpu_count=1,
        cpu_millicores=500,
        created_at=now,
    )
    idempotent = reserve_concurrency(
        counter=counter,
        existing_reservation=existing,
        workspace_id="ws-1",
        container_id="container-1",
        workspace_gpu_quota=1,
        workspace_cpu_quota_millicores=500,
        request_gpu_count=1,
        request_cpu_millicores=500,
        now=now,
    )
    assert idempotent.status is ConcurrencyReservationStatus.Ok
    assert not idempotent.changed

    repairing = reserve_concurrency(
        counter=counter.model_copy(update={"initialized": False}),
        existing_reservation=None,
        workspace_id="ws-1",
        container_id="container-2",
        workspace_gpu_quota=10,
        workspace_cpu_quota_millicores=10_000,
        request_gpu_count=1,
        request_cpu_millicores=100,
        now=now,
    )
    assert repairing.status is ConcurrencyReservationStatus.Repairing

    gpu_exceeded = reserve_concurrency(
        counter=counter,
        existing_reservation=None,
        workspace_id="ws-1",
        container_id="container-2",
        workspace_gpu_quota=1,
        workspace_cpu_quota_millicores=10_000,
        request_gpu_count=1,
        request_cpu_millicores=100,
        now=now,
    )
    assert gpu_exceeded.status is ConcurrencyReservationStatus.GpuExceeded

    cpu_exceeded = reserve_concurrency(
        counter=counter,
        existing_reservation=None,
        workspace_id="ws-1",
        container_id="container-2",
        workspace_gpu_quota=10,
        workspace_cpu_quota_millicores=600,
        request_gpu_count=1,
        request_cpu_millicores=200,
        now=now,
    )
    assert cpu_exceeded.status is ConcurrencyReservationStatus.CpuExceeded

    released = release_concurrency(
        counter=ConcurrencyCounter(workspace_id="ws-1", gpu_count=0, cpu_millicores=50),
        reservation=existing,
        now=now,
    )
    assert released.status is ConcurrencyReservationStatus.Ok
    assert released.counter.gpu_count == 0
    assert released.counter.cpu_millicores == 0


def test_worker_network_ip_repository_preserves_ownership_invariants(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repo = RedisWorkerNetworkIpRepository(redis)

    lock = repo.set_network_lock("10.10.0.0/24", ttl_seconds=5, retries=0)
    assert lock.kind is WorkerRepositoryLockKind.Network
    assert lock.acquired
    _assert_redis_ttl(redis, repo.keys.network_lock("10.10.0.0/24"), 5)
    assert not repo.set_network_lock("10.10.0.0/24", ttl_seconds=5, retries=0).acquired
    assert not repo.remove_network_lock("10.10.0.0/24", "wrong").released
    assert repo.remove_network_lock("10.10.0.0/24", lock.token).released

    assigned = repo.set_container_ip("10.10.0.0/24", "container-1", "10.10.0.8")
    assert assigned.action is NetworkIpMutationAction.Set
    assert assigned.changed
    assert repo.list_assignments("10.10.0.0/24")[0].container_id == "container-1"

    same = repo.set_container_ip("10.10.0.0/24", "container-1", "10.10.0.8")
    assert same.action is NetworkIpMutationAction.Set
    assert not same.changed

    with pytest.raises(SchedulerRepositoryError):
        repo.set_container_ip("10.10.0.0/24", "container-2", "10.10.0.8")

    moved = repo.move_container_ip("10.10.0.0/24", "container-1", "container-2", "10.10.0.8")
    assert moved.action is NetworkIpMutationAction.Move
    assert repo.get_container_ip("10.10.0.0/24", "container-1") is None
    assert repo.get_container_ip("10.10.0.0/24", "container-2") == "10.10.0.8"
    assert repo.list_ips("10.10.0.0/24") == ["10.10.0.8"]
    assert repo.list_container_network_prefixes("container-2") == ["10.10.0.0/24"]

    redis.delete(repo.keys.network_container_prefixes("container-2"))
    repo.remove_container_ips("container-2")
    assert repo.get_container_ip("10.10.0.0/24", "container-2") is None
    assert repo.list_assignments("10.10.0.0/24") == []

    owner_mismatch = plan_remove_network_container_ip(
        container_id="container-3",
        current_ip="10.10.0.9",
        owner="container-4",
    )
    assert owner_mismatch.action is NetworkIpMutationAction.Remove
    assert not owner_mismatch.cleanup_current_owner

    move_rejected = plan_move_network_container_ip(
        requested_ip="10.10.0.9",
        source_current_ip="10.10.0.9",
        target_current_ip="",
        owner="container-5",
        source_container_id="container-3",
        target_container_id="container-4",
    )
    assert move_rejected.action is NetworkIpMutationAction.Reject
    assert move_rejected.reason == "ip owner mismatch"


def test_scheduler_pool_state_service_refreshes_worker_container_and_agent_snapshots(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    pool_states = RedisWorkerPoolStateRepository(redis)
    compute = RedisComputeStateRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    machine_id = "machine-1"
    worker_id = agent_machine_worker_id(machine_id)

    compute.save_agent_token_state(
        ComputeAgentTokenState(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            token_hash="agent-hash",
            workspace_id="ws-1",
            pool=MachinePool("gpu"),
            machine_id=machine_id,
            executor=DEFAULT_PRIVATE_EXECUTOR,
            cpu_millicores=4000,
            memory_mb=8192,
            gpu_count=1,
            preflight_passed=True,
            heartbeat_confirmed=True,
            schedulable=True,
            last_join_at=now,
            last_heartbeat_at=now,
        )
    )
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=worker_id,
            pool=MachinePool("gpu"),
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            machine_id=machine_id,
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1500,
            free_memory_mib=2048,
            free_gpu_count=1,
            total_cpu_millicores=4000,
            total_memory_mib=8192,
            total_gpu_count=1,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id="pending",
            stub_id="stub-1",
            workspace_id="ws-1",
            worker_id=worker_id,
            status=SchedulerContainerStatus.Pending,
            scheduled_at=now - timedelta(seconds=5),
        )
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id="running",
            stub_id="stub-1",
            workspace_id="ws-1",
            worker_id=worker_id,
            status=SchedulerContainerStatus.Running,
            scheduled_at=now - timedelta(seconds=8),
            started_at=now - timedelta(seconds=2),
        )
    )

    states = SchedulerPoolStateService(
        workers,
        containers,
        pool_states,
        compute,
    ).refresh(
        agent_pool_configs=[
            AgentPoolConfig(
                workspace_id="ws-1",
                pool=MachinePool("gpu"),
                capacity_owner_id="11111111-1111-4111-8111-111111111111",
            )
        ],
        now=now,
    )

    state = states["11111111-1111-4111-8111-111111111111"]
    assert state.available_workers == 1
    assert state.pending_workers == 0
    assert state.pending_containers == 1
    assert state.running_containers == 1
    assert state.ready_machines == 1
    assert state.free_cpu == 1.5
    assert state.free_memory_mib == 2048
    assert state.free_gpu == 1
    assert state.scheduling_latency_ms == 5500
    assert pool_states.get_state("11111111-1111-4111-8111-111111111111") == state


def test_scheduler_pool_state_service_isolates_same_display_name_by_capacity_owner(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    pool_states = RedisWorkerPoolStateRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    owner_one = "11111111-1111-4111-8111-111111111111"
    owner_two = "22222222-2222-4222-8222-222222222222"

    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id="worker-one",
            pool=MachinePool("shared-name"),
            capacity_owner_id=owner_one,
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=1000,
            total_cpu_millicores=1000,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id="worker-two",
            pool=MachinePool("shared-name"),
            capacity_owner_id=owner_two,
            status=SchedulerWorkerStatus.Available,
            free_cpu_millicores=3000,
            total_cpu_millicores=3000,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id="owner-one-running",
            stub_id="stub-1",
            workspace_id="ws-1",
            worker_id="worker-one",
            status=SchedulerContainerStatus.Running,
            scheduled_at=now,
            started_at=now,
        )
    )

    states = SchedulerPoolStateService(workers, containers, pool_states).refresh(
        agent_pool_configs=[
            AgentPoolConfig(
                workspace_id="ws-1",
                pool=MachinePool("shared-name"),
                capacity_owner_id=owner_one,
            ),
            AgentPoolConfig(
                workspace_id="ws-2",
                pool=MachinePool("shared-name"),
                capacity_owner_id=owner_two,
            ),
        ],
        now=now,
    )

    assert set(states) == {owner_one, owner_two}
    assert states[owner_one].pool == "shared-name"
    assert states[owner_one].running_containers == 1
    assert states[owner_one].free_cpu == 1
    assert states[owner_two].pool == "shared-name"
    assert states[owner_two].running_containers == 0
    assert states[owner_two].free_cpu == 3
    assert pool_states.get_state(owner_one) == states[owner_one]
    assert pool_states.get_state(owner_two) == states[owner_two]


def _seed_hash(redis: RedisClient, key: str, model: ContractModel) -> None:
    payload = TypeAdapter(dict[str, JsonValue]).validate_json(model.model_dump_json())
    redis.hash_set(
        key,
        mapping={
            field: json.dumps(value, separators=(",", ":"), sort_keys=True)
            for field, value in payload.items()
        },
    )


def _failing_pool_state_service() -> SchedulerPoolStateService:
    return SchedulerPoolStateService(
        _FailingPoolWorkerRepository(),
        _UnusedPoolContainerRepository(),
        _UnusedPoolStateRepository(),
    )


class _FailingPoolWorkerRepository:
    def list_workers(self) -> list[SchedulerWorkerRecord]:
        raise RuntimeError("pool state unavailable")

    def list_workers_for_capacity_owner(
        self,
        capacity_owner_id: str,
    ) -> list[SchedulerWorkerRecord]:
        _ = capacity_owner_id
        raise RuntimeError("pool state unavailable")

    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None:
        _ = worker_id
        raise RuntimeError("pool state unavailable")


class _UnusedPoolContainerRepository:
    def list_by_worker(self, worker_id: str) -> list[SchedulerContainerState]:
        _ = worker_id
        return []


class _UnusedPoolStateRepository:
    def set_state(
        self,
        capacity_owner_id: str,
        state: WorkerPoolStateSnapshot,
    ) -> WorkerPoolStateSnapshot:
        _ = capacity_owner_id
        return state


class _FailureHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def mark_scheduling_failed(
        self,
        request: SchedulerWorkerRequest,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> None:
        _ = now
        self.calls.append((request.container_id, reason))


def test_orphan_sweep_settles_the_claims_a_pooled_container_was_holding(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    """A pooled container reaped as orphaned must give back what it claimed.

    A function container is started for its stub and never carries a task id, so
    every settlement path keyed on `container.task_id` is blind to it. The sweep
    marks the record `Failed` either way; if the claim is not settled with it the
    task keeps naming a dead container, which no claim query can see and no retry
    reaches, and its caller waits forever.
    """

    redis = real_redis_actors.client()
    worker_repo = RedisSchedulerWorkerRepository(redis)
    container_repo = RedisSchedulerContainerRepository(redis)
    request_service = _request_service(
        worker_repo,
        container_repo,
        failure_handler=ContainerSchedulingPersistenceService(
            isolated_services.context,
            isolated_services.events,
            isolated_services.workspace_changes,
        ),
    )
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "orphan-claim",
        kind=StubKind.Function,
        handler="pkg.jobs:handler",
        config={"image": {"image_id": "image"}},
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        # Started for the stub, holding no task id of its own — how pooling
        # starts every function container.
        pooled = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="pooled-function-container",
                image="",
                command=[],
                workspace_id=workspace_id,
                stub_id=stub.id,
                status=ContainerStatus.Running,
            )
        )
    claimed = isolated_services.tasks.create(
        "claimed-invocation",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    isolated_services.tasks.start(claimed.id, container_id=pooled.id)

    scheduler = Scheduler(
        services=isolated_services,
        workloads=SchedulerWorkloadControls(containers=request_service),
        states=SchedulerStateStores(
            orphaned_container_confirmations=RedisOrphanedContainerConfirmationRepository(redis),
        ),
        orphaned_container_reconcile_interval_seconds=0,
        orphaned_container_confirmation_seconds=60,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)

    scheduler.reconcile_orphaned_containers(now=now)
    confirmed = scheduler.reconcile_orphaned_containers(now=now + timedelta(seconds=61))

    assert confirmed == [pooled.id]
    assert isolated_services.containers.get(pooled.id).status is ContainerStatus.Failed
    settled = isolated_services.tasks.get(claimed.id)
    assert settled.container_id != pooled.id or is_terminal_task_status(settled.status), (
        f"task {settled.id} is {settled.status.value} still naming the reaped container: "
        "no claim query can see it and no retry reaches it, so its caller waits forever"
    )
