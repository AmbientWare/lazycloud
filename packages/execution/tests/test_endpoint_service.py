from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.images import CheckpointRepository
from database.repositories.orchestration import ContainerRepository
from execution.endpoints.dispatch import EndpointDispatchUnavailable
from execution.endpoints.service import EndpointControlService
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.state import SchedulerWorkerRequest
from shared.checkpoints import CheckpointRecord, CheckpointStatus
from shared.container_requests import WorkerContainerRequestPayload
from shared.containers import ContainerRecord, ContainerStatus
from shared.env import CHECKPOINT_ENABLED_ENV
from shared.http.endpoints import StartEndpointServeRequest


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


def test_endpoint_uses_latest_available_workspace_checkpoint(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "checkpoint-endpoint",
        kind=StubKind.Endpoint,
        handler="pkg.web:app",
        config={
            "image": {"image_id": "image-web"},
            "runtime": {"checkpoint_enabled": True},
        },
    )
    with isolated_services.context.database.session() as session:
        CheckpointRepository(session).create(
            CheckpointRecord(
                checkpoint_id="checkpoint-endpoint-1",
                source_container_id="source-container",
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                stub_type=StubKind.Endpoint.value,
                status=CheckpointStatus.Available,
                exposed_ports=[8001],
            )
        )
        CheckpointRepository(session).create(
            CheckpointRecord(
                checkpoint_id="checkpoint-endpoint-failed",
                source_container_id="source-container",
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                stub_type=StubKind.Endpoint.value,
                status=CheckpointStatus.CheckpointFailed,
            )
        )

    EndpointControlService(isolated_services).start_endpoint_serve(
        StartEndpointServeRequest(stub_id=stub.id)
    )

    payload = WorkerContainerRequestPayload.model_validate(scheduler.requests[0].payload)
    assert payload.checkpoint_enabled is True
    assert f"{CHECKPOINT_ENABLED_ENV}=true" in payload.env
    assert payload.checkpoint_id == "checkpoint-endpoint-1"
    assert payload.checkpoint_exposed_ports == [8001]


def test_dispatch_names_dead_capacity_instead_of_waiting_out_its_deadline(
    isolated_services: ApiServices,
) -> None:
    """A container that has already failed is an answer, not a reason to keep waiting.

    The exit code and the container to read logs from are what the caller needs; the
    alternative it replaces is holding the connection for the full ten-minute deadline
    and then reporting a timeout, which names the symptom and never the cause.

    Driving the whole wait loop here would prove no more and cost a fake scheduler:
    warmup writes a pending container, and only a scheduler moves that to failed.
    """

    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub(
        "dead-capacity-endpoint",
        kind=StubKind.Endpoint,
        handler="pkg.web:app",
        config={"image": {"image_id": "image-web"}},
    )
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="endpoint-dead",
                image="image-web",
                command=["python"],
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                status=ContainerStatus.Failed,
                exit_code=1,
            )
        )
    with pytest.raises(EndpointDispatchUnavailable, match="exit code 1"):
        EndpointControlService(isolated_services)._raise_if_capacity_is_dead(stub)
