from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubConfigUpdateValue, StubKind, StubRecord
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from database.repositories.orchestration import AutoscalerStateRepository, ContainerRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.endpoints.dispatch import ACTIVE_ENDPOINT_DISPATCH_STATUSES, EndpointDispatchStatus
from execution.endpoints.service import EndpointControlService, EndpointDispatchStateRepository
from observability.stream_state import RedisEventStreamRepository
from pydantic import JsonValue
from scheduler.autoscaling import (
    AutoscalingDriver,
    EndpointAutoscaler,
    EndpointAutoscalingDispatchObservation,
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
from shared.container_requests import WorkerContainerRequestPayload, WorkerStartupKind
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec, Resources
from shared.deployments import DeploymentKind
from shared.env import STUB_ID_ENV, STUB_TYPE_ENV
from shared.events import Event
from shared.http.endpoints import EndpointForwardRequest
from shared.timestamps import utc_now
from shared.worker_events import ENDPOINT_SCALE_DECISION_ACTION
from tests.metric_helpers import metric_value
from tests.redis_fakes import FakeRedis


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


def test_endpoint_autoscaler_scales_up_from_active_dispatch_pressure(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_endpoint_stub(
        isolated_services,
        max_containers=3,
        tasks_per_container=1,
    )
    for _ in range(3):
        _attach_dispatch(isolated_services, stub)

    results = _endpoint_autoscaler(isolated_services, redis).reconcile()

    assert len(results) == 1
    result = results[0]
    assert result.signal_value == 3
    assert result.current_containers == 0
    assert result.desired_containers == 3
    assert [action.action for action in result.actions] == ["start", "start", "start"]
    assert len(scheduler.requests) == 3
    for request in scheduler.requests:
        payload = WorkerContainerRequestPayload.model_validate(request.payload)
        assert request.stub_id == stub.id
        assert payload.startup_kind is WorkerStartupKind.Endpoint
        assert payload.entrypoint == ["python3.12", "-m", "runner.serve"]
        assert "GATEWAY_HTTP_URL=http://gateway.internal:9000" in payload.env
        assert payload.gateway_token_required is True
        assert f"{STUB_ID_ENV}={stub.id}" in payload.env
        assert f"{STUB_TYPE_ENV}={DeploymentKind.Endpoint.value}" in payload.env
    containers = _endpoint_containers(isolated_services, stub)
    assert len(containers) == 3
    assert {container.status for container in containers} == {ContainerStatus.Pending}
    snapshot = isolated_services.metrics.latest()
    metric_labels = {
        "source": "endpoint.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.Endpoint.value,
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
    assert (
        metric_value(
            snapshot.gauges,
            "autoscaler_signal",
            **metric_labels,
            signal="active_requests",
        )
        == 3
    )
    with isolated_services.context.database.session() as session:
        state = AutoscalerStateRepository(session).get(
            workspace_id=stub.workspace_id,
            target_kind=AutoscalerTargetKind.Endpoint,
            target_id=stub.id,
        )
    assert state is not None
    assert state.source == "endpoint.autoscaler"
    assert state.current_count == 0
    assert state.desired_count == 3
    assert state.signal_name == "active_requests"
    assert state.signal_value == 3
    assert state.decision == "scale-up"
    assert state.lock_acquired is True
    assert state.last_sample["active_requests"] == 3
    assert len(state.last_actions) == 3


def _assert_endpoint_autoscaler_clamps_scale_up_to_workspace_cpu_quota(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = real_redis_actors.client()
    stub = _create_endpoint_stub(
        isolated_services,
        max_containers=3,
        tasks_per_container=1,
        runtime_config={"cpu_millicores": 500, "cpu_limit_millicores": 1000},
    )
    for _ in range(3):
        _attach_dispatch(isolated_services, stub)

    result = _endpoint_autoscaler(isolated_services, redis).reconcile()[0]

    assert result.signal_value == 3
    assert result.current_containers == 0
    assert result.desired_containers == 2
    assert result.reason == "workspace cpu quota reached"
    assert [action.action for action in result.actions] == ["start", "start"]
    assert result.guardrails["limited"] is True
    assert result.guardrails["available_start_count"] == 2
    with isolated_services.context.database.session() as session:
        state = AutoscalerStateRepository(session).get(
            workspace_id=stub.workspace_id,
            target_kind=AutoscalerTargetKind.Endpoint,
            target_id=stub.id,
        )
    assert state is not None
    assert state.reason == "workspace cpu quota reached"
    guardrails = _json_object(state.last_sample["guardrails"], "last_sample.guardrails")
    assert guardrails["limited"] is True


def _assert_endpoint_autoscaler_halts_scale_up_after_failed_container_threshold(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_endpoint_stub(
        isolated_services,
        max_containers=3,
        tasks_per_container=1,
        failed_container_threshold=2,
        failure_window_seconds=300,
    )
    current_time = utc_now()
    newest_failed_id = "00000000-0000-4000-8000-000000000301"
    older_failed_id = "00000000-0000-4000-8000-000000000302"
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
        "00000000-0000-4000-8000-000000000303",
        finished_at=current_time - timedelta(seconds=600),
    )
    _attach_dispatch(isolated_services, stub)
    _attach_dispatch(isolated_services, stub)

    result = _endpoint_autoscaler(isolated_services, redis).reconcile(now=current_time)[0]

    assert result.signal_value == 2
    assert result.current_containers == 0
    assert result.desired_containers == 0
    assert result.reason == "failed container threshold reached"
    assert result.failed_containers == [newest_failed_id, older_failed_id]
    assert result.actions == []
    assert scheduler.requests == []


def test_endpoint_autoscaler_persists_scale_decisions_only_on_transition(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_endpoint_stub(
        isolated_services,
        max_containers=3,
        tasks_per_container=1,
    )
    service = _endpoint_autoscaler(isolated_services, redis)

    def decision_events() -> list[Event]:
        return isolated_services.events.list(
            workspace_id=stub.workspace_id,
            actions=(ENDPOINT_SCALE_DECISION_ACTION,),
        )

    service.reconcile()
    service.reconcile()
    assert len(decision_events()) == 1

    _attach_dispatch(isolated_services, stub)
    service.reconcile()
    events = decision_events()
    assert len(events) == 2
    assert events[0].data["decision"] == "scale-up"


def _create_endpoint_stub(
    runtime: ApiServices,
    *,
    max_containers: int,
    tasks_per_container: int,
    keep_warm_seconds: int = 0,
    runtime_config: dict[str, JsonValue] | None = None,
    failed_container_threshold: int | None = None,
    failure_window_seconds: int | None = None,
) -> StubRecord:
    deployment = runtime.deployments.deploy(
        DeploymentSpec(
            name="endpoint-autoscale",
            kind=DeploymentKind.Endpoint,
            handler="pkg.endpoint:handler",
            resources=Resources(timeout_seconds=30, concurrency=1, keep_warm=keep_warm_seconds),
        )
    )
    autoscaler: dict[str, JsonValue] = {
        "max_containers": max_containers,
        "tasks_per_container": tasks_per_container,
    }
    if failed_container_threshold is not None:
        autoscaler["failed_container_threshold"] = failed_container_threshold
    if failure_window_seconds is not None:
        autoscaler["failure_window_seconds"] = failure_window_seconds
    stub_runtime_config: dict[str, JsonValue] = {
        "timeout_seconds": 30,
        "concurrency": 1,
        "keep_warm": keep_warm_seconds,
    }
    if runtime_config is not None:
        stub_runtime_config.update(runtime_config)
    control = ControlPlaneService(runtime.context)
    stub = next(
        item
        for item in control.list_stubs()
        if item.deployment_id == deployment.id and item.kind is StubKind.Endpoint
    )
    fields: dict[str, StubConfigUpdateValue] = {
        "image": {"image_id": "img-endpoint"},
        "runtime": stub_runtime_config,
        "autoscaler": autoscaler,
    }
    return control.update_stub_config(
        stub.id,
        fields=fields,
    ).stub


def _attach_dispatch(
    runtime: ApiServices,
    stub: StubRecord,
    *,
    status: EndpointDispatchStatus = EndpointDispatchStatus.Queued,
    container_id: str | None = None,
) -> None:
    task = runtime.tasks.create(
        "endpoint-dispatch",
        deployment_id=stub.deployment_id,
        handler=stub.handler,
    )
    repository = EndpointDispatchStateRepository(runtime)
    repository.attach(
        task,
        stub=stub,
        request=EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}"),
        wait_timeout_seconds=30,
        max_pending_requests=100,
        max_inflight_per_container=1,
    )
    if status is EndpointDispatchStatus.Queued:
        return
    repository.transition(task, EndpointDispatchStatus.Inflight, container_id=container_id)
    if status is not EndpointDispatchStatus.Inflight:
        repository.transition(task, status)


def _json_object(value: JsonValue, name: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{name} must be a JSON object"
    return value


def _endpoint_containers(services: ApiServices, stub: StubRecord) -> list[ContainerRecord]:
    return [container for container in services.containers.list() if container.stub_id == stub.id]


def _endpoint_autoscaler(
    services: ApiServices,
    redis: RedisClient,
) -> AutoscalingDriver:
    return AutoscalingDriver(
        services,
        redis=redis,
        workload=EndpointAutoscaler(
            services,
            endpoints=EndpointControlService(
                services,
                gateway_http_url=lambda: "http://gateway.internal:9000",
            ),
            dispatches=_EndpointDispatchReader(EndpointDispatchStateRepository(services)),
        ),
        container_states=RedisSchedulerContainerRepository(redis),
        container_requests=RedisSchedulerWorkerRepository(redis),
    )


@dataclass(frozen=True, slots=True)
class _EndpointDispatchReader:
    repository: EndpointDispatchStateRepository

    def active_count(self, stub_id: str) -> int:
        return self.repository.active_count(stub_id)

    def list_by_stub(self, stub_id: str) -> list[EndpointAutoscalingDispatchObservation]:
        return [
            EndpointAutoscalingDispatchObservation(
                container_id=record.container_id,
                active=record.status in ACTIVE_ENDPOINT_DISPATCH_STATUSES,
                finished_at=record.finished_at,
            )
            for record in self.repository.list_by_stub(stub_id)
        ]


def _record_container(
    runtime: ApiServices,
    stub: StubRecord,
    container_id: str,
    *,
    created_at: datetime,
) -> ContainerRecord:
    container = ContainerRecord(
        id=container_id,
        name=f"endpoint-{container_id}",
        image="img-endpoint",
        command=["python3.12", "-m", "runner.serve"],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        app_id=stub.app_id,
        status=ContainerStatus.Running,
        created_at=created_at,
        started_at=created_at,
    )
    with runtime.context.database.session() as session:
        return ContainerRepository(session).upsert(container)


def _record_failed_container(
    runtime: ApiServices,
    stub: StubRecord,
    container_id: str,
    *,
    finished_at: datetime,
) -> ContainerRecord:
    container = ContainerRecord(
        id=container_id,
        name=f"endpoint-{container_id}",
        image="img-endpoint",
        command=["python3.12", "-m", "runner.serve"],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        app_id=stub.app_id,
        status=ContainerStatus.Failed,
        exit_code=1,
        created_at=finished_at - timedelta(seconds=1),
        started_at=finished_at - timedelta(milliseconds=500),
        finished_at=finished_at,
    )
    with runtime.context.database.session() as session:
        return ContainerRepository(session).upsert(container)


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
        workspace_owners=DatabaseWorkspaceOwners(services.context),
    )
