from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind, StubRecord
from coordination.redis_client import RedisClient
from database.repositories.orchestration import ContainerRepository
from execution.pods.service import PodControlService
from fastapi.testclient import TestClient
from identity.auth import AuthService
from scheduler.autoscaling import PodAutoscalingService
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.state import SchedulerWorkerRequest
from shared.container_requests import WorkerContainerRequestPayload
from shared.containers import ContainerStatus
from shared.http.pods import CreatePodRequest, PodSandboxUpdateTTLRequest
from shared.timestamps import utc_now
from shared.workload_keys import pod_keep_warm_lock_key
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis
from tests.scheduler_composition import services_with_redis_container_control
from worker.checkpoints import (
    WorkerCheckpointStatus,
    create_checkpoint_state_payload,
)
from worker_repository.checkpoint_records import CheckpointService


def test_sandbox_create_refresh_and_terminate_own_the_durable_ttl_lock(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    redis = real_redis_actors.client()
    real_services = services_with_redis_container_control(isolated_services, redis)
    services = replace(
        real_services,
        containers=replace(real_services.containers, scheduler=scheduler),
    )
    stub = _sandbox_stub(services, keep_warm_seconds=60)
    service = PodControlService(services, redis=redis)

    created = service.create_pod(CreatePodRequest(stub_id=stub.id))
    lock_key = redis.key(pod_keep_warm_lock_key(stub.workspace_id, stub.id, created.container_id))

    assert redis.exists(lock_key)
    assert 0 < redis.ttl(lock_key) <= 60
    assert scheduler.requests[0].stub_id == stub.id

    service.sandbox_update_ttl(
        created.container_id,
        PodSandboxUpdateTTLRequest(ttl=120),
    )
    assert 0 < redis.ttl(lock_key) <= 120

    service.sandbox_update_ttl(
        created.container_id,
        PodSandboxUpdateTTLRequest(ttl=-1),
    )
    assert redis.exists(lock_key)
    assert redis.ttl(lock_key) == -1
    persistent = services.containers.get(created.container_id)
    assert persistent.timeout_seconds == -1
    assert persistent.expires_at is None

    token, _ = AuthService(services.context).create_token(
        "sandbox-lifecycle",
        scopes=["read", "write"],
        workspace_id="default",
    )
    with TestClient(create_app(services, pod_service=service)) as client:
        response = client.post(
            f"/api/v1/pods/{created.container_id}/terminate",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 204
        assert services.containers.get(created.container_id).status is ContainerStatus.Stopped
        assert not redis.exists(lock_key)


def test_scheduler_expires_prepared_sandbox_without_a_deployment(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    redis = real_redis_actors.client()
    real_services = services_with_redis_container_control(isolated_services, redis)
    services = replace(
        real_services,
        containers=replace(real_services.containers, scheduler=scheduler),
    )
    stub = _sandbox_stub(services, keep_warm_seconds=60)
    pod_service = PodControlService(services, redis=redis)
    created = pod_service.create_pod(CreatePodRequest(stub_id=stub.id))
    started_at = utc_now()
    container = services.containers.get(created.container_id)
    container.status = ContainerStatus.Running
    container.started_at = started_at
    with services.context.database.session() as session:
        ContainerRepository(session).upsert(container)

    autoscaler = PodAutoscalingService(services, redis=redis, pods=pod_service)
    held = autoscaler.reconcile(now=utc_now())

    assert len(held) == 1
    assert held[0].stub_id == stub.id
    assert held[0].desired_containers == 0
    assert held[0].actions == []
    assert services.containers.get(container.id).status is ContainerStatus.Running

    lock_key = redis.key(pod_keep_warm_lock_key(stub.workspace_id, stub.id, container.id))
    pod_service.sandbox_update_ttl(
        container.id,
        PodSandboxUpdateTTLRequest(ttl=1),
    )
    redis.delete(lock_key)
    expired = autoscaler.reconcile(now=started_at + timedelta(seconds=2))

    assert [action.container_id for action in expired[0].actions] == [container.id], expired[
        0
    ].model_dump_json()
    assert expired[0].actions[0].action == "stop"
    assert services.containers.get(container.id).status is ContainerStatus.Stopped


def test_sandbox_restore_resolves_source_stub_and_schedules_typed_checkpoint(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    service = PodControlService(
        services,
        redis=RedisClient(FakeRedis(), key_prefix="test"),
    )
    stub = _sandbox_stub(services, keep_warm_seconds=60)
    workspace = ControlPlaneService(services.context).get_workspace(stub.workspace_id)
    checkpoint = CheckpointService(services.context).save_state(
        create_checkpoint_state_payload(
            checkpoint_id="checkpoint-warm",
            source_container_id="container-source",
            status=WorkerCheckpointStatus.Available,
            workspace_id=workspace.id,
            stub_id=stub.id,
            stub_type=StubKind.Sandbox.value,
            cache_hash="a" * 64,
            cache_size_bytes=1024,
            origin_key="checkpoints/checkpoint-warm.tar",
        )
    )

    created = service.create_pod(
        CreatePodRequest(checkpoint_id=checkpoint.checkpoint_id),
        authorized_workspace_id=workspace.id,
    )

    request = scheduler.requests[-1]
    payload = WorkerContainerRequestPayload.model_validate(request.payload)
    assert created.stub_id == stub.id
    assert request.stub_id == stub.id
    assert payload.checkpoint_id == checkpoint.checkpoint_id


def _sandbox_stub(services: ApiServices, *, keep_warm_seconds: int) -> StubRecord:
    control = ControlPlaneService(services.context)
    stub = control.create_stub("sandbox-lifecycle", kind=StubKind.Sandbox)
    return control.update_stub_config(
        stub.id,
        fields={
            "runtime": {"keep_warm": keep_warm_seconds},
            "command": ["tail", "-f", "/dev/null"],
        },
    ).stub


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
