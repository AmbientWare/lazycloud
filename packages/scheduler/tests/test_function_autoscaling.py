"""What the function autoscaler owes its operator, on the same terms as the others.

A function scales on a different signal from an endpoint or a pod, and that is
the only thing about it that should differ. Pausing it has to stop it, its
decisions have to reach the history an operator reads, and a tick has to leave
the metrics and the state row every other autoscaler leaves — otherwise the
control surface answers for two kinds of workload and silently omits the third.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubConfigUpdateValue, StubKind, StubRecord
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from database.repositories.orchestration import AutoscalerStateRepository, ContainerRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.functions.service import FunctionControlService
from observability.stream_state import RedisEventStreamRepository
from pydantic import JsonValue
from scheduler.autoscaling import (
    CONTAINER_START_DEADLINE_SECONDS,
    AutoscalingDriver,
    FunctionAutoscaler,
)
from scheduler.containers import (
    CONTAINER_DISPATCH_WAKE_SCOPE,
    SchedulerContainerRequestService,
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
)
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    SchedulerWorkerRequest,
)
from scheduler.workspace_owners import DatabaseWorkspaceOwners
from shared.autoscaler_state import AutoscalerTargetKind
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec, Resources
from shared.deployments import DeploymentKind
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import FunctionInvokeBody
from shared.scheduling import SchedulerContainerState, SchedulerContainerStatus
from shared.tasks import TaskStatus
from shared.timestamps import utc_now
from tests.metric_helpers import metric_value
from tests.redis_fakes import FakeRedis


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


class _IdentityPlacement:
    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        return request


class _Scheduler:
    def __init__(self) -> None:
        self.requests: list[SchedulerWorkerRequest] = []

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: object = None,
    ) -> SchedulerContainerSubmitResult:
        del ready_at
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
        )


def test_paused_function_autoscaler_leaves_its_backlog_alone(
    isolated_services: ApiServices,
) -> None:
    """Pause stops the autoscaler that provisions a function, or it stops nothing.

    An operator pauses an autoscaler to stop capacity moving while they look at
    something. The flag is one flag over all three kinds, so a function that
    keeps scaling through it makes the whole control read as advisory.
    """

    scheduler = _Scheduler()
    services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_function_stub(services, max_containers=3)
    _enqueue_invocations(services, stub, count=3)
    # The first container is the invocation's to start, not the autoscaler's, so
    # what pause has to stop is every container after this one.
    started_before_pause = len(scheduler.requests)
    services.autoscaler_operations_service.pause(stub.id, workspace=stub.workspace_id)

    results = _function_autoscaler(services, redis).reconcile()

    # A paused stub is not held, it is not looked at: the same thing the
    # endpoint and pod autoscalers have always done with one.
    assert results == []
    assert len(scheduler.requests) == started_before_pause


def test_function_autoscaler_records_what_it_decided(
    isolated_services: ApiServices,
) -> None:
    """A function's tick leaves the same evidence an endpoint's or a pod's does.

    The state row, the metrics and the history are one operator surface. A kind
    missing from any of them is not a quieter autoscaler, it is one nobody can
    answer questions about.
    """

    scheduler = _Scheduler()
    services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_function_stub(services, max_containers=3)
    _enqueue_invocations(services, stub, count=3)

    results = _function_autoscaler(services, redis).reconcile()

    assert len(results) == 1
    result = results[0]
    # One container is already up: an invocation of an idle stub may start the
    # first, and the autoscaler owns every one after it.
    assert result.current_containers == 1
    assert result.desired_containers == 3
    # "start" for every kind: the action is a metric label, and one event that
    # carried two names could not be counted.
    assert [action.action for action in result.actions] == ["start", "start"]

    metric_labels = {
        "source": "function.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.Function.value,
    }
    assert (
        metric_value(
            "autoscaler_decisions_total",
            **metric_labels,
            decision="scale-up",
        )
        == 1
    )
    assert metric_value("autoscaler_desired_containers", **metric_labels) == 3
    assert (
        metric_value(
            "autoscaler_signal",
            **metric_labels,
            signal="unclaimed_tasks",
        )
        == 3
    )

    with services.context.database.session() as session:
        state = AutoscalerStateRepository(session).get(
            workspace_id=stub.workspace_id,
            target_kind=AutoscalerTargetKind.Function,
            target_id=stub.id,
        )
    assert state is not None
    assert state.signal_name == "unclaimed_tasks"
    assert state.signal_value == 3
    assert state.decision == "scale-up"
    assert len(state.last_actions) == 2

    history = services.autoscaler_operations_service.history(
        workspace=stub.workspace_id,
        target_id=stub.id,
    )
    assert [event.action for event in history.events] == ["function.autoscaler.scale_decision"]


def test_function_autoscaler_reclaims_a_container_that_never_started(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    """A pending record nothing will start must stop holding the ceiling.

    This is the deadlock it exists for: `max_containers` is counted from the
    durable rows, so one row stuck at `pending` is one permanent unit of
    capacity, and at a ceiling of one the backlog grows a task per schedule fire
    with nothing left able to start. Recovery did eventually arrive from the
    container's scheduler state lapsing and its worker noticing, roughly sixteen
    minutes on — owned by a cache expiry, and by a worker still being alive to
    read it.

    The container still starting is in the same case on purpose. It is what
    makes the rule a deadline rather than a reflex: reaping one that is merely
    pulling would free the ceiling, start a replacement, and reap that one at
    the same age.
    """

    scheduler = _Scheduler()
    redis = real_redis_actors.client()
    services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler=scheduler,
            scheduler_cancellation=_scheduler_request_service(isolated_services, redis),
        ),
    )
    stub = _create_function_stub(services, max_containers=2)
    _enqueue_invocations(services, stub, count=3)
    # Started by the invocation moments ago and not yet running anywhere: the
    # container a shorter rule would throw away.
    starting = _pending_containers(services, stub)
    assert len(starting) == 1
    current_time = utc_now()
    stranded = _record_pending_container(
        services,
        stub,
        created_at=current_time - timedelta(seconds=CONTAINER_START_DEADLINE_SECONDS + 1),
    )
    # Present, pending, and going nowhere — the state a half-alive worker
    # re-arms indefinitely, so no expiry is coming to settle this row.
    RedisSchedulerContainerRepository(redis).set_container_state(
        SchedulerContainerState(
            container_id=stranded.id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            status=SchedulerContainerStatus.Pending,
        )
    )

    result = _function_autoscaler(services, redis).reconcile(now=current_time)[0]

    assert [
        action.container_id for action in result.actions if action.action == "recover-stale"
    ] == [stranded.id]
    assert services.containers.get(stranded.id).status is ContainerStatus.Stopped
    assert services.containers.get(starting[0].id).status is ContainerStatus.Pending
    # The slot the stranded row held is the one the backlog gets.
    assert result.current_containers == 1
    assert result.desired_containers == 2
    assert result.actions[-1].action == "start"


def test_function_startup_breaker_fails_queued_tasks_with_the_startup_error(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_function_stub(services, max_containers=1)
    invoked = FunctionControlService(services).function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=FunctionJsonInvocation(args=[1]),
        )
    )
    now = utc_now()
    startup_error = (
        "container startup failed during load-image: image OCI descriptor does not belong "
        "to the workload registry"
    )
    starting = _pending_containers(services, stub)[0]
    starting.status = ContainerStatus.Failed
    starting.exit_code = 1
    starting.finished_at = now
    starting.startup_error = startup_error
    with services.context.database.session() as session:
        repository = ContainerRepository(session)
        repository.upsert(starting)
        for suffix in (902, 903):
            repository.upsert(
                ContainerRecord(
                    id=f"00000000-0000-4000-8000-000000000{suffix}",
                    name="function-startup-failure",
                    image="img-function",
                    command=["python", "-m", "runner"],
                    workspace_id=stub.workspace_id,
                    stub_id=stub.id,
                    app_id=stub.app_id,
                    status=ContainerStatus.Failed,
                    exit_code=1,
                    startup_error=startup_error,
                    created_at=now,
                    finished_at=now,
                )
            )

    result = _function_autoscaler(services, redis).reconcile(now=now)[0]

    failed_task = services.tasks.get(invoked.task_id)
    assert failed_task.status is TaskStatus.Failed
    assert failed_task.error == startup_error
    assert failed_task.exit_code == 1
    assert len(scheduler.requests) == 1
    assert [action.action for action in result.actions] == ["fail-unclaimed-tasks"]
    assert FunctionControlService(services).schedule_due_retries(now=now) == []
    assert len(scheduler.requests) == 1


def _create_function_stub(runtime: ApiServices, *, max_containers: int) -> StubRecord:
    deployment = runtime.deployments.deploy(
        DeploymentSpec(
            name="function-autoscale",
            kind=DeploymentKind.Function,
            handler="pkg.function:handler",
            resources=Resources(timeout_seconds=30, concurrency=1),
        )
    )
    control = ControlPlaneService(runtime.context)
    stub = next(
        item
        for item in control.list_stubs()
        if item.deployment_id == deployment.id and item.kind is StubKind.Function
    )
    autoscaler: dict[str, JsonValue] = {
        "max_containers": max_containers,
        "tasks_per_container": 1,
    }
    fields: dict[str, StubConfigUpdateValue] = {
        "image": {"image_id": "img-function"},
        "runtime": {"timeout_seconds": 30, "concurrency": 1},
        "autoscaler": autoscaler,
    }
    return control.update_stub_config(stub.id, fields=fields).stub


def _enqueue_invocations(runtime: ApiServices, stub: StubRecord, *, count: int) -> None:
    """Leave a backlog the autoscaler has to answer for.

    Invoked through the function service rather than written as task rows: the
    first call may start a container of its own, and a backlog the autoscaler
    reads has to be the one invocations actually leave behind.
    """

    functions = FunctionControlService(runtime)
    for value in range(count):
        functions.function_invoke(
            FunctionInvokeBody(
                stub_id=stub.id,
                invocation=FunctionJsonInvocation(args=[value]),
            )
        )


def _function_autoscaler(services: ApiServices, redis: RedisClient) -> AutoscalingDriver:
    return AutoscalingDriver(
        services,
        redis=redis,
        workload=FunctionAutoscaler(services, functions=FunctionControlService(services)),
        container_states=RedisSchedulerContainerRepository(redis),
        container_requests=RedisSchedulerWorkerRepository(redis),
    )


def _pending_containers(services: ApiServices, stub: StubRecord) -> list[ContainerRecord]:
    return [
        container
        for container in services.containers.list(workspace_id=stub.workspace_id)
        if container.stub_id == stub.id and container.status is ContainerStatus.Pending
    ]


def _record_pending_container(
    services: ApiServices,
    stub: StubRecord,
    *,
    created_at: datetime,
) -> ContainerRecord:
    container = ContainerRecord(
        id="00000000-0000-4000-8000-000000000901",
        name="function-stranded",
        image="img-function",
        command=["python", "-m", "runner"],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        app_id=stub.app_id,
        status=ContainerStatus.Pending,
        created_at=created_at,
    )
    with services.context.database.session() as session:
        return ContainerRepository(session).upsert(container)


def _scheduler_request_service(
    services: ApiServices,
    redis: RedisClient,
) -> SchedulerContainerRequestService:
    persistence = ContainerSchedulingPersistenceService(
        services.context,
        services.events,
        services.workspace_changes,
    )
    return SchedulerContainerRequestService(
        RedisSchedulerWorkerRepository(redis),
        RedisSchedulerContainerRepository(redis),
        placement=_IdentityPlacement(),
        failure_handler=persistence,
        assignments=persistence,
        dispatch_wake=RedisWakeSignal(redis, CONTAINER_DISPATCH_WAKE_SCOPE),
        lifecycle_events=RedisEventStreamRepository(redis),
        workspace_owners=DatabaseWorkspaceOwners(services.context),
    )
