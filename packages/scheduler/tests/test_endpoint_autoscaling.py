from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubConfigUpdateValue, StubKind, StubRecord
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from database.records.endpoint_dispatch import EndpointDispatchStateRecord
from database.repositories.endpoint_dispatch import EndpointDispatchRepository
from database.repositories.orchestration import AutoscalerStateRepository, ContainerRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.endpoints.dispatch import EndpointDispatchStatus
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
from shared.timestamps import utc_now
from shared.worker_events import ENDPOINT_SCALE_DECISION_ACTION
from tests.metric_helpers import metric_value
from tests.redis_fakes import FakeRedis


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
    metric_labels = {
        "source": "endpoint.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.Endpoint.value,
    }
    assert (
        metric_value(
            "autoscaler_decisions_total",
            **metric_labels,
            decision="scale-up",
        )
        == 1
    )
    assert metric_value("autoscaler_current_containers", **metric_labels) == 0
    assert metric_value("autoscaler_desired_containers", **metric_labels) == 3
    assert (
        metric_value(
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
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        deployment_id=stub.deployment_id,
        handler=stub.handler,
    )
    repository = EndpointDispatchStateRepository(runtime)
    now = utc_now()
    with runtime.context.database.session() as session:
        EndpointDispatchRepository(session).create(
            EndpointDispatchStateRecord(
                task_id=task.id,
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                container_id=None,
                method="POST",
                path="/",
                status=EndpointDispatchStatus.Queued.value,
                wait_timeout_seconds=30,
                max_pending_requests=100,
                max_inflight_per_container=1,
                attempts=0,
                enqueued_at=now,
                started_at=None,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=30),
                finished_at=None,
                error=None,
            )
        )
    if status is EndpointDispatchStatus.Queued:
        return
    repository.transition(task, EndpointDispatchStatus.Inflight, container_id=container_id)
    if status is not EndpointDispatchStatus.Inflight:
        repository.transition(task, status)


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

    def active_counts_by_stub(self, stub_ids: Sequence[str]) -> dict[str, int]:
        return self.repository.active_counts_by_stub(stub_ids)

    def observations_by_stub(
        self,
        stub_ids: Sequence[str],
        *,
        finished_since: datetime,
    ) -> dict[str, list[EndpointAutoscalingDispatchObservation]]:
        return {
            stub_id: [
                EndpointAutoscalingDispatchObservation(
                    container_id=record.container_id,
                    active=record.active,
                    finished_at=record.finished_at,
                )
                for record in records
            ]
            for stub_id, records in self.repository.observations_by_stub(
                stub_ids,
                finished_since=finished_since,
            ).items()
        }


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
