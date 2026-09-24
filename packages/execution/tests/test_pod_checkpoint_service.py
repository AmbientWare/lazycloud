from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from coordination.redis_client import RedisClient
from database.repositories.images import CheckpointRepository
from database.repositories.storage import ObjectRepository
from execution.pods.service import PodControlService
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.state import SchedulerWorkerRequest
from shared.checkpoints import CheckpointRecord, CheckpointStatus
from shared.container_requests import WORKER_USER_CODE_VOLUME, WorkerContainerRequestPayload
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.pods import CreatePodRequest
from shared.objects import ObjectRecord
from tests.redis_fakes import FakeRedis
from tests.workspaces import on_team_plan


class _Scheduler:
    def __init__(self) -> None:
        self.requests: list[SchedulerWorkerRequest] = []

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        del ready_at
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
        )


def test_checkpoint_enabled_pod_uses_latest_available_checkpoint(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    stub = ControlPlaneService(
        isolated_services.context,
    ).create_stub(
        "checkpoint-pod",
        kind=StubKind.Pod,
        config={
            "image": {"image_id": "image-pod"},
            "command": ["python", "-m", "app"],
            "runtime": {
                "checkpoint_enabled": True,
                "checkpoint_readiness_path": "/ready",
                "checkpoint_readiness_port": 8000,
            },
        },
    )
    with isolated_services.context.database.session() as session:
        checkpoints = CheckpointRepository(session)
        checkpoints.create(
            CheckpointRecord(
                checkpoint_id="checkpoint-pod-available",
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                stub_type=StubKind.Pod.value,
                status=CheckpointStatus.Available,
                exposed_ports=[8000],
            )
        )
        checkpoints.create(
            CheckpointRecord(
                checkpoint_id="checkpoint-pod-failed",
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                stub_type=StubKind.Pod.value,
                status=CheckpointStatus.CheckpointFailed,
            )
        )

    PodControlService(
        isolated_services,
        redis=RedisClient(FakeRedis(), key_prefix="test"),
    ).create_pod(CreatePodRequest(stub_id=stub.id))

    payload = WorkerContainerRequestPayload.model_validate(scheduler.requests[0].payload)
    assert payload.checkpoint_enabled is True
    assert payload.checkpoint_id == "checkpoint-pod-available"
    assert payload.checkpoint_exposed_ports == [8000]


def test_a_devbox_starts_on_its_disk_without_the_uploaded_source(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    control = ControlPlaneService(isolated_services.context)
    source = ObjectRecord(
        id=str(uuid4()), bucket="default", key="source", path="/objects/source", size=1, sha256="0"
    )
    with isolated_services.context.database.session() as session:
        ObjectRepository(session).upsert(source, workspace_id=control.get_workspace().id)
    on_team_plan(isolated_services.database, control.get_workspace().id)
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(name="box", kind=DeploymentKind.Pod)
    )
    stub = control.create_stub(
        "box",
        kind=StubKind.Pod,
        deployment_id=deployment.id,
        config={
            "object_id": source.id,
            "image": {"image_id": "image-devbox"},
            "role": "devbox",
            "ssh": True,
            "disks": [{"name": "box", "size_bytes": 1 << 30}],
        },
    )

    PodControlService(
        isolated_services,
        redis=RedisClient(FakeRedis(), key_prefix="test"),
    ).create_pod(CreatePodRequest(stub_id=stub.id))

    payload = WorkerContainerRequestPayload.model_validate(scheduler.requests[0].payload)
    assert payload.cwd == "/root"
    assert all(mount.mount_path != WORKER_USER_CODE_VOLUME for mount in payload.mounts)
