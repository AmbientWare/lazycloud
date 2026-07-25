from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from coordination.redis_client import RedisClient
from database.repositories.images import CheckpointRepository
from execution.pods.service import PodControlService
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.state import SchedulerWorkerRequest
from shared.checkpoints import CheckpointRecord, CheckpointStatus
from shared.container_requests import WorkerContainerRequestPayload
from shared.http.pods import CreatePodRequest
from tests.redis_fakes import FakeRedis


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
    stub = ControlPlaneService(isolated_services.context).create_stub(
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
                source_container_id="source-container",
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
                source_container_id="source-container",
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
