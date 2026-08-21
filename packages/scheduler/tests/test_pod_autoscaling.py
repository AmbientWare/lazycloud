from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubConfigUpdateValue, StubKind, StubRecord
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from database.repositories.apps import StubRepository
from database.repositories.orchestration import AutoscalerStateRepository, ContainerRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.pods.proxy import PodProxySession, PodProxyTarget
from execution.pods.service import PodControlService
from gateway.pod_proxy import RedisPodProxyConnectionRepository
from observability.stream_state import RedisEventStreamRepository
from operations.management import ManagementService
from pydantic import JsonValue
from scheduler.autoscaling import AutoscalingDriver, PodAutoscaler
from scheduler.containers import (
    CONTAINER_DISPATCH_WAKE_SCOPE,
    SchedulerContainerRequestService,
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
)
from scheduler.fleet import SchedulerContainerStatus
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    SchedulerContainerState,
    SchedulerWorkerRequest,
)
from scheduler.workspace_owners import DatabaseWorkspaceOwners
from shared.autoscaler_state import AutoscalerTargetKind
from shared.autoscaling import PodStubType
from shared.container_requests import WorkerContainerRequestPayload, WorkerStartupKind
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec, Resources
from shared.deployments import DeploymentKind
from shared.errors import InvalidInputError
from shared.timestamps import utc_now
from shared.workload_keys import (
    pod_container_connections_key,
    pod_keep_warm_lock_key,
    pod_total_connections_key,
)
from tests.metric_helpers import metric_value
from tests.redis_fakes import FakeRedis


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


def test_pod_autoscaler_scales_immediately_idle_deployment_to_zero(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_pod_stub(isolated_services, keep_warm_seconds=0)

    result = _pod_autoscaler(isolated_services, redis).reconcile()[0]

    assert result.signal_value == 0
    assert result.current_containers == 0
    assert result.desired_containers == 0
    assert result.actions == []
    assert scheduler.requests == []
    metric_labels = {
        "source": "pod.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.Pod.value,
    }
    assert (
        metric_value(
            "autoscaler_decisions_total",
            **metric_labels,
            decision="hold",
        )
        == 1
    )
    assert metric_value("autoscaler_current_containers", **metric_labels) == 0
    assert metric_value("autoscaler_desired_containers", **metric_labels) == 0
    assert (
        metric_value(
            "autoscaler_signal",
            **metric_labels,
            signal="total_connections",
        )
        == 0
    )
    with isolated_services.context.database.session() as session:
        state = AutoscalerStateRepository(session).get(
            workspace_id=stub.workspace_id,
            target_kind=AutoscalerTargetKind.Pod,
            target_id=stub.id,
        )
    assert state is not None
    assert state.source == "pod.autoscaler"
    assert state.current_count == 0
    assert state.desired_count == 0
    assert state.signal_name == "total_connections"
    assert state.signal_value == 0
    assert state.decision == "hold"
    assert state.lock_acquired is True
    assert state.last_sample["total_connections"] == 0
    assert state.last_actions == []


def test_pod_autoscaler_replaces_running_records_without_live_scheduler_state(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = real_redis_actors.client()
    isolated_services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler_cancellation=_scheduler_request_service(isolated_services, redis),
        ),
    )
    stub = _create_pod_stub(
        isolated_services,
        keep_warm_seconds=0,
        autoscaler={"min_containers": 1, "max_containers": 1},
    )
    current_time = utc_now()
    missing = _record_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000201",
        created_at=current_time - timedelta(seconds=60),
        redis=redis,
        state_status=None,
    )
    terminal = _record_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000202",
        created_at=current_time - timedelta(seconds=60),
        redis=redis,
        state_status=SchedulerContainerStatus.Complete,
    )

    result = _pod_autoscaler(isolated_services, redis).reconcile(now=current_time)[0]

    assert result.current_containers == 0
    assert result.desired_containers == 1
    assert {
        action.container_id for action in result.actions if action.action == "recover-stale"
    } == {missing.id, terminal.id}
    assert result.actions[-1].action == "start"
    assert isolated_services.containers.get(missing.id).status is ContainerStatus.Stopped
    assert isolated_services.containers.get(terminal.id).status is ContainerStatus.Stopped
    assert len(scheduler.requests) == 1


def test_pod_keep_warm_minus_one_is_durable_never_scale_to_zero(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="pod-always-on",
            kind=DeploymentKind.Pod,
            resources=Resources(keep_warm=-1),
        )
    )
    control = ControlPlaneService(isolated_services.context)
    stub = next(item for item in control.list_stubs() if item.deployment_id == deployment.id)

    assert stub.config.runtime.keep_warm == -1
    assert stub.config.autoscaler.min_containers == 1
    assert stub.config.autoscaler.max_containers == 1

    result = _pod_autoscaler(isolated_services, redis).reconcile()[0]
    assert result.desired_containers == 1
    assert [action.action for action in result.actions] == ["start"]
    payload = WorkerContainerRequestPayload.model_validate(scheduler.requests[0].payload)
    assert payload.startup_kind is WorkerStartupKind.Pod
    assert payload.stub_type == PodStubType.PodDeployment.value
    assert "KEEP_WARM_SECONDS=-1" in payload.env

    with pytest.raises(InvalidInputError, match="always-on pod deployments"):
        ManagementService(isolated_services).scale_deployment(
            "default",
            deployment.id,
            containers=0,
        )

    with pytest.raises(ValueError, match="only supported for pod"):
        DeploymentSpec(
            name="function-invalid",
            kind=DeploymentKind.Function,
            resources=Resources(keep_warm=-1),
        )


def test_always_on_pod_deployment_releases_containers_the_operator_scaled_away(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = real_redis_actors.client()
    isolated_services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler_cancellation=_scheduler_request_service(isolated_services, redis),
        ),
    )
    stub = _create_pod_stub(
        isolated_services,
        keep_warm_seconds=-1,
        autoscaler={"min_containers": 2, "max_containers": 2},
    )
    current_time = utc_now()
    kept = _record_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000407",
        created_at=current_time - timedelta(seconds=120),
        redis=redis,
    )
    released = _record_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000408",
        created_at=current_time - timedelta(seconds=60),
        redis=redis,
    )
    # An always-on pod holds its keep-warm lock with no expiry, so both
    # containers carry one for as long as they run.
    locks = {
        container.id: redis.key(pod_keep_warm_lock_key(stub.workspace_id, stub.id, container.id))
        for container in (kept, released)
    }
    for lock_key in locks.values():
        redis.set(lock_key, "1")

    deployment_id = stub.deployment_id
    assert deployment_id is not None
    ManagementService(isolated_services).scale_deployment(
        "default",
        deployment_id,
        containers=1,
    )
    result = _pod_autoscaler(isolated_services, redis).reconcile(now=current_time)[0]

    assert result.current_containers == 2
    assert result.desired_containers == 1
    assert [action.container_id for action in result.actions] == [released.id]
    assert isolated_services.containers.get(released.id).status is ContainerStatus.Stopped
    assert isolated_services.containers.get(kept.id).status is ContainerStatus.Running
    assert not redis.exists(locks[released.id])
    assert redis.exists(locks[kept.id])


def test_pod_autoscaler_scales_down_only_idle_deployment_containers(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = real_redis_actors.client()
    isolated_services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler_cancellation=_scheduler_request_service(isolated_services, redis),
        ),
    )
    stub = _create_pod_stub(
        isolated_services,
        keep_warm_seconds=60,
        autoscaler={"min_containers": 1, "max_containers": 4},
    )
    current_time = utc_now()
    busy = _record_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000401",
        created_at=current_time - timedelta(seconds=120),
        redis=redis,
    )
    locked = _record_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000402",
        created_at=current_time - timedelta(seconds=120),
        redis=redis,
    )
    warm = _record_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000403",
        created_at=current_time - timedelta(seconds=10),
        redis=redis,
    )
    idle = _record_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000404",
        created_at=current_time - timedelta(seconds=120),
        redis=redis,
    )
    workspace_id = stub.workspace_id
    redis.set(redis.key(pod_container_connections_key(workspace_id, stub.id, busy.id)), 2)
    redis.set(redis.key(pod_keep_warm_lock_key(workspace_id, stub.id, locked.id)), "1")

    result = _pod_autoscaler(isolated_services, redis).reconcile(now=current_time)[0]

    assert result.signal_value == 0
    assert result.current_containers == 4
    assert result.desired_containers == 1
    assert [action.container_id for action in result.actions] == [idle.id]
    assert isolated_services.containers.get(idle.id).status is ContainerStatus.Stopped
    assert isolated_services.containers.get(busy.id).status is ContainerStatus.Running
    assert isolated_services.containers.get(locked.id).status is ContainerStatus.Running
    assert isolated_services.containers.get(warm.id).status is ContainerStatus.Running


def test_pod_last_proxy_disconnect_renews_idle_window_before_autoscaler_stop(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = real_redis_actors.client()
    isolated_services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler_cancellation=_scheduler_request_service(isolated_services, redis),
        ),
    )
    stub = _create_pod_stub(isolated_services, keep_warm_seconds=2)
    container = _record_container(
        isolated_services,
        stub,
        "00000000-0000-4000-8000-000000000405",
        created_at=utc_now() - timedelta(seconds=60),
        redis=redis,
    )
    connections = RedisPodProxyConnectionRepository(redis)
    service = PodControlService(
        isolated_services,
        redis=redis,
        pod_proxy_connections=connections,
    )
    lock_key = redis.key(pod_keep_warm_lock_key(stub.workspace_id, stub.id, container.id))
    redis.set(lock_key, "1", ex=1)
    connections.increment_total_connections(stub.workspace_id, stub.id)
    connections.increment_container_connections(
        stub.workspace_id,
        stub.id,
        container.id,
        keep_warm_seconds=stub.config.runtime.keep_warm,
    )
    time.sleep(1.1)

    assert redis.ttl(lock_key) == -1
    active = _pod_autoscaler(isolated_services, redis).reconcile()[0]
    assert active.signal_value == 1
    assert active.actions == []

    service.finish_pod_proxy(
        PodProxySession(
            workspace_id=stub.workspace_id,
            stub_id=stub.id,
            target=PodProxyTarget(container_id=container.id, address="10.0.0.1:8080"),
            keep_warm_seconds=stub.config.runtime.keep_warm,
        )
    )

    assert 1 <= redis.ttl(lock_key) <= 2
    idle_but_warm = _pod_autoscaler(isolated_services, redis).reconcile()[0]
    assert idle_but_warm.signal_value == 0
    assert idle_but_warm.desired_containers == 0
    assert idle_but_warm.actions == []
    assert isolated_services.containers.get(container.id).status is ContainerStatus.Running

    redis.delete(lock_key)
    cooled = _pod_autoscaler(isolated_services, redis).reconcile()[0]
    assert [action.container_id for action in cooled.actions] == [container.id]
    assert isolated_services.containers.get(container.id).status is ContainerStatus.Stopped


@pytest.mark.parametrize("keep_warm_seconds", [2, -1])
def test_pod_proxy_finalization_is_idempotent_after_stub_deletion(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
    keep_warm_seconds: int,
) -> None:
    redis = real_redis_actors.client()
    stub = _create_pod_stub(isolated_services, keep_warm_seconds=keep_warm_seconds)
    connections = RedisPodProxyConnectionRepository(redis)
    service = PodControlService(
        isolated_services,
        redis=redis,
        pod_proxy_connections=connections,
    )
    container_id = "00000000-0000-4000-8000-000000000406"
    session = PodProxySession(
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        target=PodProxyTarget(container_id=container_id, address="10.0.0.1:8080"),
        keep_warm_seconds=stub.config.runtime.keep_warm,
    )
    connections.increment_total_connections(stub.workspace_id, stub.id)
    lock_key = redis.key(pod_keep_warm_lock_key(stub.workspace_id, stub.id, container_id))
    redis.set(lock_key, "1")
    connections.increment_container_connections(
        stub.workspace_id,
        stub.id,
        container_id,
        keep_warm_seconds=stub.config.runtime.keep_warm,
    )
    assert redis.ttl(lock_key) == -1
    with isolated_services.context.database.session() as database_session:
        assert StubRepository(database_session).delete(
            stub.id,
            workspace_id=stub.workspace_id,
        )
    redis.delete(lock_key)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(service.finish_pod_proxy, [session] * 8))

    assert not redis.exists(lock_key)
    assert connections.container_connections(stub.workspace_id, stub.id, container_id) == 0
    assert not redis.exists(redis.key(pod_total_connections_key(stub.workspace_id, stub.id)))


def _assert_pod_autoscaler_clamps_scale_up_to_workspace_cpu_quota(
    isolated_services: ApiServices,
    real_redis_actors: _RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = real_redis_actors.client()
    stub = _create_pod_stub(
        isolated_services,
        keep_warm_seconds=0,
        autoscaler={"max_containers": 3},
        resource_config={"cpu_millicores": 500, "workspace_cpu_quota_millicores": 1000},
    )
    redis.set(redis.key(pod_total_connections_key(stub.workspace_id, stub.id)), 4)

    result = _pod_autoscaler(isolated_services, redis).reconcile()[0]

    assert result.signal_value == 4
    assert result.current_containers == 0
    assert result.desired_containers == 2
    assert result.reason == "workspace cpu quota reached"
    assert [action.action for action in result.actions] == ["start", "start"]
    assert result.guardrails["limited"] is True
    assert result.guardrails["available_start_count"] == 2
    with isolated_services.context.database.session() as session:
        state = AutoscalerStateRepository(session).get(
            workspace_id=stub.workspace_id,
            target_kind=AutoscalerTargetKind.Pod,
            target_id=stub.id,
        )
    assert state is not None
    assert state.reason == "workspace cpu quota reached"
    guardrails = _json_object(state.last_sample["guardrails"], "last_sample.guardrails")
    assert guardrails["limited"] is True


def _assert_pod_autoscaler_halts_scale_up_after_failed_container_threshold(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_pod_stub(
        isolated_services,
        keep_warm_seconds=0,
        autoscaler={
            "max_containers": 3,
            "failed_container_threshold": 2,
            "failure_window_seconds": 300,
        },
    )
    current_time = utc_now()
    newest_failed_id = "00000000-0000-4000-8000-000000000501"
    older_failed_id = "00000000-0000-4000-8000-000000000502"
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
        "00000000-0000-4000-8000-000000000503",
        finished_at=current_time - timedelta(seconds=600),
    )
    redis.set(redis.key(pod_total_connections_key(stub.workspace_id, stub.id)), 4)

    result = _pod_autoscaler(isolated_services, redis).reconcile(now=current_time)[0]

    assert result.signal_value == 4
    assert result.current_containers == 0
    assert result.desired_containers == 0
    assert result.reason == "failed container threshold reached"
    assert result.failed_containers == [newest_failed_id, older_failed_id]
    assert result.actions == []
    assert scheduler.requests == []


def test_pod_deployment_explicit_zero_scale_remains_zero_with_connections(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_pod_stub(isolated_services, keep_warm_seconds=120)
    deployment_id = stub.deployment_id or ""
    redis.set(redis.key(pod_total_connections_key(stub.workspace_id, stub.id)), 4)

    ManagementService(isolated_services).scale_deployment(
        "default",
        deployment_id,
        containers=0,
    )

    updated = ControlPlaneService(isolated_services.context).get_stub(stub.id)
    assert updated.config.autoscaler.model_fields_set >= {"min_containers", "max_containers"}
    assert updated.config.autoscaler.min_containers == 0
    assert updated.config.autoscaler.max_containers == 0
    result = _pod_autoscaler(isolated_services, redis).reconcile()[0]
    assert result.signal_value == 4
    assert result.desired_containers == 0
    assert result.actions == []
    assert scheduler.requests == []


def _create_pod_stub(
    services: ApiServices,
    *,
    keep_warm_seconds: int,
    autoscaler: dict[str, JsonValue] | None = None,
    resource_config: dict[str, JsonValue] | None = None,
) -> StubRecord:
    deployment = services.deployments.deploy(
        DeploymentSpec(
            name="pod-autoscale",
            kind=DeploymentKind.Pod,
            resources=Resources(keep_warm=keep_warm_seconds),
            command=["python", "-m", "http.server"],
            ports={"8080": 8080},
        )
    )
    control = ControlPlaneService(services.context)
    stub = next(
        item
        for item in control.list_stubs()
        if item.deployment_id == deployment.id and item.kind is StubKind.Pod
    )
    runtime_config: dict[str, JsonValue] = {"keep_warm": keep_warm_seconds}
    if resource_config is not None:
        runtime_config.update(resource_config)
    fields: dict[str, StubConfigUpdateValue] = {
        "image": {"image_id": "img-pod"},
        "runtime": runtime_config,
        "command": ["python", "-m", "http.server"],
        "ports": {"8080": 8080},
        "autoscaler": autoscaler or {},
    }
    return control.update_stub_config(
        stub.id,
        fields=fields,
    ).stub


def _pod_autoscaler(services: ApiServices, redis: RedisClient) -> AutoscalingDriver:
    return AutoscalingDriver(
        services,
        redis=redis,
        workload=PodAutoscaler(
            services,
            redis=redis,
            pods=PodControlService(services, redis=redis),
        ),
        container_states=RedisSchedulerContainerRepository(redis),
        container_requests=RedisSchedulerWorkerRepository(redis),
    )


def _json_object(value: JsonValue, name: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{name} must be a JSON object"
    return value


def _record_container(
    services: ApiServices,
    stub: StubRecord,
    container_id: str,
    *,
    created_at: datetime,
    redis: RedisClient,
    state_status: SchedulerContainerStatus | None = SchedulerContainerStatus.Running,
) -> ContainerRecord:
    """A running container as the platform holds one: the row and the state.

    Both, because the autoscaler counts capacity from the durable row and only
    trusts it as far as the scheduler still backs it. A row written on its own is
    the stranded record, not the healthy one, so `state_status=None` is how a
    test asks for that.
    """

    container = ContainerRecord(
        id=container_id,
        name=f"pod-{container_id}",
        image="img-pod",
        command=["python", "-m", "http.server"],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        app_id=stub.app_id,
        status=ContainerStatus.Running,
        created_at=created_at,
        started_at=created_at,
        ports={"8080": 8080},
    )
    if state_status is not None:
        RedisSchedulerContainerRepository(redis).set_container_state(
            SchedulerContainerState(
                container_id=container_id,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
                status=state_status,
            )
        )
    with services.context.database.session() as session:
        return ContainerRepository(session).upsert(container)


def _record_failed_container(
    services: ApiServices,
    stub: StubRecord,
    container_id: str,
    *,
    finished_at: datetime,
) -> ContainerRecord:
    container = ContainerRecord(
        id=container_id,
        name=f"pod-{container_id}",
        image="img-pod",
        command=["python", "-m", "http.server"],
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
