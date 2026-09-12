from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.images import CheckpointRepository
from database.repositories.orchestration import ContainerRepository
from execution.endpoints.service import EndpointControlService
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.state import SchedulerWorkerRequest
from shared.checkpoints import CheckpointRecord, CheckpointStatus
from shared.container_requests import WorkerContainerRequestPayload
from shared.containers import ContainerStatus
from shared.env import CHECKPOINT_ENABLED_ENV
from shared.http.endpoints import EndpointForwardRequest, EndpointWarmupRequest


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


class _FailingScheduler(_Scheduler):
    def __init__(self, services: ApiServices) -> None:
        super().__init__()
        self.services = services

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        result = super().submit(request, ready_at=ready_at)
        with self.services.context.database.session() as session:
            repository = ContainerRepository(session)
            container = repository.get_across_workspaces(request.container_id)
            assert container is not None
            container.status = ContainerStatus.Failed
            container.exit_code = 1
            repository.upsert(container)
        return result


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

    EndpointControlService(isolated_services).warm_endpoint(EndpointWarmupRequest(stub_id=stub.id))

    payload = WorkerContainerRequestPayload.model_validate(scheduler.requests[0].payload)
    assert payload.checkpoint_enabled is True
    assert f"{CHECKPOINT_ENABLED_ENV}=true" in payload.env
    assert payload.checkpoint_id == "checkpoint-endpoint-1"
    assert payload.checkpoint_exposed_ports == [8001]


@pytest.mark.anyio
async def test_dispatch_names_dead_capacity_instead_of_waiting_out_its_deadline(
    isolated_services: ApiServices,
) -> None:
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "dead-capacity-endpoint",
        kind=StubKind.Endpoint,
        handler="pkg.web:app",
        config={
            "image": {"image_id": "image-web"},
            "runtime": {"timeout_seconds": 0.2},
        },
    )
    services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler=_FailingScheduler(isolated_services),
        ),
    )
    composed_endpoint = isolated_services.endpoint_service
    assert isinstance(composed_endpoint, EndpointControlService)
    dispatcher = composed_endpoint.async_dispatcher
    assert dispatcher is not None
    async_io = isolated_services.require_async_io()
    service = EndpointControlService(
        services,
        async_database=async_io.database,
        async_dispatcher=dispatcher,
    )
    try:
        response = await service.forward_endpoint_request(
            EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
        )
    finally:
        await async_io.close()

    assert response.status_code == 503
    assert b"no container could start for this endpoint (exit code 1)" in response.body
