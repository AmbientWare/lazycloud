from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pickle import dumps
from typing import Protocol

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind, StubRecord
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from database.repositories.execution import QueueRepository
from database.repositories.orchestration import AutoscalerStateRepository, ContainerRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.taskqueues.service import TaskQueueControlService
from observability.stream_state import RedisEventStreamRepository
from pydantic import JsonValue
from scheduler.autoscaling import TaskQueueAutoscalingService
from scheduler.containers import CONTAINER_DISPATCH_WAKE_SCOPE, SchedulerContainerRequestService
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
)
from shared.autoscaler_state import AutoscalerTargetKind
from shared.container_requests import WorkerContainerRequestPayload, WorkerStartupKind
from shared.containers import ContainerRecord, ContainerStatus
from shared.env import STUB_ID_ENV
from shared.scheduling import (
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
    SchedulerWorkerRequest,
)
from shared.tasks import TaskStatus
from shared.timestamps import utc_now
from tests.metric_helpers import metric_value
from tests.redis_fakes import FakeRedis


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


def test_task_queue_autoscaler_scales_up_from_queue_depth(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    service = TaskQueueControlService(isolated_services, redis=redis)
    stub = _create_task_queue_stub(
        isolated_services,
        max_containers=3,
        tasks_per_container=2,
    )
    _publish_tasks(service, stub, count=5)

    results = TaskQueueAutoscalingService(
        isolated_services,
        redis=redis,
        task_queues=service,
    ).reconcile()

    assert len(results) == 1
    result = results[0]
    assert result.queue_length == 5
    assert result.current_containers == 0
    assert result.desired_containers == 3
    assert [action.action for action in result.actions] == ["start", "start", "start"]
    assert len(scheduler.requests) == 3
    for request in scheduler.requests:
        payload = WorkerContainerRequestPayload.model_validate(request.payload)
        assert request.stub_id == stub.id
        assert payload.startup_kind is WorkerStartupKind.TaskQueue
        assert f"{STUB_ID_ENV}={stub.id}" in payload.env
        assert "KEEP_WARM_SECONDS=0" in payload.env
    containers = _task_queue_containers(isolated_services, stub)
    assert len(containers) == 3
    assert {container.status for container in containers} == {ContainerStatus.Pending}
    snapshot = isolated_services.metrics.latest()
    metric_labels = {
        "source": "taskqueue.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.TaskQueue.value,
    }
    assert (
        metric_value(
            snapshot.counters,
            "autoscaler_decisions_total",
            **metric_labels,
            decision="scale-up",
        )
        == 1
    )
    assert metric_value(snapshot.gauges, "autoscaler_current_containers", **metric_labels) == 0
    assert metric_value(snapshot.gauges, "autoscaler_desired_containers", **metric_labels) == 3
    assert metric_value(snapshot.gauges, "autoscaler_pending_containers", **metric_labels) == 0
    assert metric_value(snapshot.gauges, "autoscaler_idle_containers", **metric_labels) == 0
    assert metric_value(snapshot.gauges, "autoscaler_max_containers", **metric_labels) == 3
    assert (
        metric_value(
            snapshot.gauges,
            "autoscaler_signal",
            **metric_labels,
            signal="queue_length",
        )
        == 5
    )
    assert (
        metric_value(
            snapshot.gauges,
            "autoscaler_pressure_saturated",
            **metric_labels,
            signal="queue_length",
        )
        == 0
    )
    assert (
        metric_value(
            snapshot.counters,
            "autoscaler_actions_total",
            **metric_labels,
            action="start",
        )
        == 3
    )
    with isolated_services.context.database.session() as session:
        state = AutoscalerStateRepository(session).get(
            workspace_id=stub.workspace_id,
            target_kind=AutoscalerTargetKind.TaskQueue,
            target_id=stub.id,
        )
    assert state is not None
    assert state.source == "taskqueue.autoscaler"
    assert state.current_count == 0
    assert state.desired_count == 3
    assert state.signal_name == "queue_length"
    assert state.signal_value == 5
    assert state.decision == "scale-up"
    assert state.reason == "queue-pending"
    assert state.lock_acquired is True
    assert state.last_sample["running_tasks"] == 0
    assert len(state.last_actions) == 3


def test_task_queue_autoscaler_expires_pending_work_before_scaling(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    service = TaskQueueControlService(isolated_services, redis=redis)
    stub = _create_task_queue_stub(
        isolated_services,
        max_containers=1,
        tasks_per_container=1,
    )
    put = service.task_queue_put(
        stub.id,
        dumps({"args": (1,), "kwargs": {}}),
    )
    current_time = utc_now()
    with isolated_services.context.database.session() as session:
        repository = QueueRepository(session)
        message = next(
            item
            for item in repository.messages.list(workspace_id=stub.workspace_id)
            if item.queue == f"taskqueue:{stub.id}"
        )
        message.expires_at = current_time - timedelta(seconds=1)
        repository.upsert_message(message, workspace_id=stub.workspace_id)

    result = TaskQueueAutoscalingService(
        isolated_services,
        redis=redis,
        task_queues=service,
    ).reconcile(now=current_time)[0]

    assert result.queue_length == 0
    assert result.desired_containers == 0
    assert scheduler.requests == []
    assert isolated_services.tasks.get(put.task_id).status is TaskStatus.Expired
    with isolated_services.context.database.session() as session:
        messages = QueueRepository(session).messages.list(workspace_id=stub.workspace_id)
    assert all(message.queue != f"taskqueue:{stub.id}" for message in messages)


def test_task_queue_autoscaler_reports_pending_container_metrics(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    service = TaskQueueControlService(isolated_services, redis=redis)
    stub = _create_task_queue_stub(
        isolated_services,
        max_containers=1,
        tasks_per_container=1,
    )
    _publish_tasks(service, stub, count=2)
    autoscaler = TaskQueueAutoscalingService(
        isolated_services,
        redis=redis,
        task_queues=service,
    )
    autoscaler.reconcile()

    result = autoscaler.reconcile()[0]

    assert result.pending_containers == 1
    snapshot = isolated_services.metrics.latest()
    metric_labels = {
        "source": "taskqueue.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.TaskQueue.value,
    }
    assert metric_value(snapshot.gauges, "autoscaler_pending_containers", **metric_labels) == 1
    assert (
        metric_value(
            snapshot.gauges,
            "autoscaler_pressure_saturated",
            **metric_labels,
            signal="queue_length",
        )
        == 1
    )


def _assert_task_queue_autoscaler_clamps_scale_up_to_workspace_cpu_quota(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = real_redis_actors.client()
    service = TaskQueueControlService(isolated_services, redis=redis)
    stub = _create_task_queue_stub(
        isolated_services,
        max_containers=3,
        tasks_per_container=1,
        runtime_config={"cpu_millicores": 500, "cpu_limit_millicores": 1000},
    )
    _publish_tasks(service, stub, count=3)

    result = TaskQueueAutoscalingService(
        isolated_services,
        redis=redis,
        task_queues=service,
    ).reconcile()[0]

    assert result.current_containers == 0
    assert result.desired_containers == 2
    assert result.reason == "workspace cpu quota reached"
    assert [action.action for action in result.actions] == ["start", "start"]
    assert result.guardrails["limited"] is True
    assert result.guardrails["available_start_count"] == 2
    snapshot = isolated_services.metrics.latest()
    metric_labels = {
        "source": "taskqueue.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.TaskQueue.value,
    }
    assert metric_value(snapshot.gauges, "autoscaler_guardrail_throttled", **metric_labels) == 1
    assert (
        metric_value(
            snapshot.counters,
            "autoscaler_throttled_total",
            **metric_labels,
            reason="workspace cpu quota reached",
        )
        == 1
    )
    with isolated_services.context.database.session() as session:
        state = AutoscalerStateRepository(session).get(
            workspace_id=stub.workspace_id,
            target_kind=AutoscalerTargetKind.TaskQueue,
            target_id=stub.id,
        )
    assert state is not None
    assert state.reason == "workspace cpu quota reached"
    guardrails = _json_object(state.last_sample["guardrails"], "last_sample.guardrails")
    assert guardrails["limited"] is True


def _assert_task_queue_autoscaler_halts_scale_up_after_failed_container_threshold(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    service = TaskQueueControlService(isolated_services, redis=redis)
    stub = _create_task_queue_stub(
        isolated_services,
        max_containers=3,
        tasks_per_container=1,
        failed_container_threshold=2,
        failure_window_seconds=300,
    )
    current_time = utc_now()
    newest_failed_id = "00000000-0000-4000-8000-000000000101"
    older_failed_id = "00000000-0000-4000-8000-000000000102"
    _record_failed_container(
        isolated_services,
        stub,
        newest_failed_id,
        finished_at=current_time - timedelta(seconds=5),
    )
    _record_failed_container(
        isolated_services,
        stub,
        older_failed_id,
        finished_at=current_time - timedelta(seconds=10),
    )
    _record_failed_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000103",
        finished_at=current_time - timedelta(seconds=600),
    )
    _publish_tasks(service, stub, count=3)

    result = TaskQueueAutoscalingService(
        isolated_services,
        redis=redis,
        task_queues=service,
    ).reconcile(now=current_time)[0]

    assert result.queue_length == 3
    assert result.current_containers == 0
    assert result.desired_containers == 0
    assert result.reason == "failed container threshold reached"
    assert result.failed_containers == [newest_failed_id, older_failed_id]
    assert result.actions == []
    assert scheduler.requests == []
    snapshot = isolated_services.metrics.latest()
    metric_labels = {
        "source": "taskqueue.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.TaskQueue.value,
    }
    assert metric_value(snapshot.gauges, "autoscaler_failed_containers", **metric_labels) == 2
    assert (
        metric_value(
            snapshot.gauges,
            "autoscaler_failed_container_threshold_reached",
            **metric_labels,
        )
        == 1
    )


def test_task_queue_autoscaler_records_scale_failure_capacity_metrics(
    isolated_services: ApiServices,
) -> None:
    scheduler = _FailingScheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    service = TaskQueueControlService(isolated_services, redis=redis)
    stub = _create_task_queue_stub(
        isolated_services,
        max_containers=1,
        tasks_per_container=1,
    )
    _publish_tasks(service, stub, count=1)

    result = TaskQueueAutoscalingService(
        isolated_services,
        redis=redis,
        task_queues=service,
    ).reconcile()[0]

    assert [action.action for action in result.actions] == ["scale-up-failed"]
    snapshot = isolated_services.metrics.latest()
    metric_labels = {
        "source": "taskqueue.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.TaskQueue.value,
    }
    assert (
        metric_value(
            snapshot.counters,
            "autoscaler_scale_failures_total",
            **metric_labels,
            action="scale-up-failed",
        )
        == 1
    )
    assert (
        metric_value(snapshot.counters, "autoscaler_no_worker_capacity_total", **metric_labels) == 1
    )
    assert metric_value(snapshot.gauges, "autoscaler_no_worker_capacity", **metric_labels) == 1


def _create_task_queue_stub(
    runtime: ApiServices,
    *,
    max_containers: int,
    tasks_per_container: int,
    runtime_config: dict[str, JsonValue] | None = None,
    failed_container_threshold: int | None = None,
    failure_window_seconds: int | None = None,
) -> StubRecord:
    autoscaler: dict[str, JsonValue] = {
        "max_containers": max_containers,
        "tasks_per_container": tasks_per_container,
    }
    if failed_container_threshold is not None:
        autoscaler["failed_container_threshold"] = failed_container_threshold
    if failure_window_seconds is not None:
        autoscaler["failure_window_seconds"] = failure_window_seconds
    runtime_payload: dict[str, JsonValue] = {}
    if runtime_config is not None:
        runtime_payload.update(runtime_config)
    config: dict[str, JsonValue] = {
        "image": {"image_id": "img-taskqueue"},
        "runtime": runtime_payload,
        "autoscaler": autoscaler,
    }
    return ControlPlaneService(runtime.context).create_stub(
        "queue-autoscale",
        kind=StubKind.TaskQueue,
        handler="pkg.queue:handler",
        config=config,
    )


def _publish_tasks(service: TaskQueueControlService, stub: StubRecord, *, count: int) -> None:
    for index in range(count):
        put = service.task_queue_put(
            stub.id,
            dumps({"args": (index,), "kwargs": {}}),
        )
        assert put.task_id


def _task_queue_containers(services: ApiServices, stub: StubRecord) -> list[ContainerRecord]:
    return [container for container in services.containers.list() if container.stub_id == stub.id]


def _json_object(value: JsonValue, name: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{name} must be a JSON object"
    return value


def _mark_running(
    services: ApiServices,
    containers: list[ContainerRecord],
) -> list[ContainerRecord]:
    updated: list[ContainerRecord] = []
    with services.context.database.session() as session:
        repository = ContainerRepository(session)
        for container in containers:
            updated.append(
                repository.upsert(container.model_copy(update={"status": ContainerStatus.Running}))
            )
    updated.sort(key=lambda item: item.id)
    return updated


def _record_failed_container(
    services: ApiServices,
    stub: StubRecord,
    container_id: str,
    *,
    finished_at: datetime,
) -> ContainerRecord:
    container = ContainerRecord(
        id=container_id,
        name=f"taskqueue-{container_id}",
        image="img-taskqueue",
        command=["python3.12", "-m", "runner.taskqueue"],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        app_id=stub.app_id,
        status=ContainerStatus.Failed,
        exit_code=1,
        created_at=finished_at - timedelta(seconds=1),
        started_at=finished_at - timedelta(milliseconds=500),
        finished_at=finished_at,
    )
    with services.context.database.session() as session:
        return ContainerRepository(session).upsert(container)


def _delete_task_queue_messages(services: ApiServices, stub: StubRecord) -> None:
    with services.context.database.session() as session:
        messages = QueueRepository(session).messages
        for message in messages.list(workspace_id=stub.workspace_id):
            if message.queue == f"taskqueue:{stub.id}":
                messages.delete(message.id, workspace_id=stub.workspace_id)


class _Scheduler:
    def __init__(self) -> None:
        self.requests: list[SchedulerWorkerRequest] = []

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        _ = ready_at
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
            reason="queued",
        )


class _FailingScheduler:
    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        _ = ready_at
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Error,
            container_id=request.container_id,
            reason="no worker capacity available",
        )


class _IdentityPlacement:
    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest:
        return request


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
    )
