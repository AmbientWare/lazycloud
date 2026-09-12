from __future__ import annotations

from datetime import timedelta

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.apps import StubRepository
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.container_scheduling import ContainerSchedulingRepository
from database.repositories.orchestration import ContainerRepository
from database.repositories.previews import PreviewSessionRepository
from database.tables.endpoint_dispatch import EndpointDispatchTable
from execution.containers.service import ContainerService
from execution.endpoints.previews import PreviewSessionService
from execution.endpoints.service import EndpointControlService
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployments import StubKind
from shared.errors import ConflictError, NotFoundError
from shared.http.endpoints import EndpointForwardRequest, EndpointWarmupRequest
from shared.http.previews import CreatePreviewRequest, PreviewSessionResponse, PreviewSessionStatus
from shared.scheduling import SchedulerWorkerRequest
from shared.timestamps import utc_now
from sqlalchemy import func, select


@pytest.mark.anyio
async def test_preview_stop_fences_its_url_and_preserves_source_and_sibling(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    control = ControlPlaneService(services.context)
    source = control.create_stub(
        "preview-source",
        kind=StubKind.Endpoint,
        handler="module:handler",
        config={"image": {"image_id": "image-web"}},
    )
    endpoint = services.endpoint_service
    assert isinstance(endpoint, EndpointControlService)
    first = endpoint.create_preview(
        CreatePreviewRequest(stub_id=source.id), workspace_id=source.workspace_id
    )
    second = endpoint.create_preview(
        CreatePreviewRequest(stub_id=source.id), workspace_id=source.workspace_id
    )
    assert first.execution_stub_id is not None
    assert first.execution_stub_id not in {source.id, second.execution_stub_id}
    assert first.container_id and second.container_id
    assert first.expires_at is None
    previews = PreviewSessionService(services)
    endpoint.stop_preview(first.id, workspace_id=source.workspace_id)
    stopped = endpoint.get_preview(first.id, workspace_id=source.workspace_id)
    assert stopped.status is PreviewSessionStatus.Stopped
    assert services.containers.get(first.container_id).status is ContainerStatus.Stopped
    assert endpoint.get_preview(second.id).status is PreviewSessionStatus.Active
    assert services.containers.get(second.container_id).status is ContainerStatus.Pending
    assert control.get_stub(source.id) == source
    with services.context.database.session() as session:
        assert ContainerRolloutRepository(session).admission_closed_at(first.container_id)
        selected = StubRepository(session).list_autoscaling_across_workspaces()
        assert source.id in {item.id for item in selected}
        assert first.execution_stub_id not in {item.id for item in selected}
        assert second.execution_stub_id not in {item.id for item in selected}
    assert not services.redis_client.exists(previews.lease_key(first.id))
    with pytest.raises(NotFoundError):
        endpoint.renew_preview(first.id, workspace_id=source.workspace_id)
    with pytest.raises(NotFoundError):
        endpoint.warm_endpoint(EndpointWarmupRequest(stub_id=first.execution_stub_id))
    for preview_id in (None, first.id):
        response = await endpoint.forward_endpoint_request(
            EndpointForwardRequest(
                stub_id=first.execution_stub_id,
                preview_session_id=preview_id,
                method="POST",
                body=b"{}",
            )
        )
        assert response.status_code == 404
    with services.context.database.session() as session:
        assert (
            session.scalar(
                select(func.count()).where(EndpointDispatchTable.stub_id == first.execution_stub_id)
            )
            == 0
        )
    assert (
        len(
            [item for item in services.containers.list() if item.stub_id == first.execution_stub_id]
        )
        == 1
    )
    endpoint.stop_preview(second.id, workspace_id=source.workspace_id)


def test_preview_deadline_and_lost_lease_cannot_be_renewed(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    source = ControlPlaneService(services.context).create_stub(
        "preview-expiry",
        kind=StubKind.Asgi,
        handler="module:app",
        config={"image": {"image_id": "image-web"}},
    )
    previews = PreviewSessionService(services)
    deadline = previews.create(
        CreatePreviewRequest(stub_id=source.id, timeout=3), workspace_id=source.workspace_id
    )
    execution = previews.execution_stub(deadline.id)
    assert deadline.expires_at == deadline.created_at + timedelta(seconds=3)
    with services.context.database.session() as session:
        row = PreviewSessionRepository(session).get(deadline.id, lock=True)
        assert row is not None
        row.expires_at = utc_now() - timedelta(seconds=1)
    endpoint = services.endpoint_service
    assert isinstance(endpoint, EndpointControlService)
    with pytest.raises(NotFoundError, match="expired"):
        endpoint._start_endpoint_container(execution, preview_id=deadline.id)
    assert previews.get(deadline.id).container_id is None
    with pytest.raises(NotFoundError):
        previews.renew(deadline.id, workspace_id=source.workspace_id)
    assert previews.get(deadline.id).status is PreviewSessionStatus.Expired
    lost = previews.create(
        CreatePreviewRequest(stub_id=source.id), workspace_id=source.workspace_id
    )
    services.redis_client.delete(previews.lease_key(lost.id))
    with pytest.raises(NotFoundError):
        previews.renew(lost.id, workspace_id=source.workspace_id)
    assert previews.get(lost.id).status is PreviewSessionStatus.Expired
    assert not services.redis_client.exists(previews.lease_key(lost.id))


def test_stop_fences_a_container_bound_after_its_read_and_recovers_delivery_failure(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = isolated_services
    source = ControlPlaneService(services.context).create_stub(
        "preview-race",
        kind=StubKind.Endpoint,
        handler="module:handler",
        config={"image": {"image_id": "image-web"}},
    )
    endpoint = services.endpoint_service
    assert isinstance(endpoint, EndpointControlService)
    previews = PreviewSessionService(services)
    preview = previews.create(
        CreatePreviewRequest(stub_id=source.id), workspace_id=source.workspace_id
    )
    execution = previews.execution_stub(preview.id)
    original_get = PreviewSessionService.get
    original_stop = type(services.containers).stop
    bound_container_id = ""

    def bind_after_read(
        self: PreviewSessionService,
        preview_id: str,
        *,
        workspace_id: str | None = None,
        public: bool = False,
    ) -> PreviewSessionResponse:
        nonlocal bound_container_id
        record = original_get(self, preview_id, workspace_id=workspace_id, public=public)
        if preview_id == preview.id and record.container_id is None and not bound_container_id:
            bound_container_id = endpoint._start_endpoint_container(
                execution, preview_id=preview.id
            ).container_id
        return record

    def stop_transport_unavailable(
        self: ContainerService,
        container_id: str,
        *,
        reason: StopContainerReason | None = None,
        only_if_pending: bool = False,
    ) -> ContainerRecord:
        del self, reason, only_if_pending
        raise ConnectionError(f"stop delivery unavailable for {container_id}")

    monkeypatch.setattr(PreviewSessionService, "get", bind_after_read)
    monkeypatch.setattr(type(services.containers), "stop", stop_transport_unavailable)
    with pytest.raises(ConnectionError, match="stop delivery unavailable"):
        previews.stop(preview.id, workspace_id=source.workspace_id)
    monkeypatch.setattr(PreviewSessionService, "get", original_get)
    assert bound_container_id
    assert previews.get(preview.id).status is PreviewSessionStatus.Stopped
    with services.context.database.session() as session:
        assert ContainerRolloutRepository(session).admission_closed_at(bound_container_id)
        assert not ContainerRolloutRepository(session).accepting_work(
            bound_container_id, stub_id=execution.id
        )
        container = ContainerRepository(session).get_across_workspaces(bound_container_id)
        assert container is not None and container.status is ContainerStatus.Pending
        with pytest.raises(ConflictError, match="admission is closed"):
            ContainerSchedulingRepository(session).submit(
                SchedulerWorkerRequest(
                    container_id=bound_container_id,
                    workspace_id=source.workspace_id,
                    stub_id=execution.id,
                ),
                now=utc_now(),
            )
    sibling = endpoint.create_preview(
        CreatePreviewRequest(stub_id=source.id), workspace_id=source.workspace_id
    )
    services.redis_client.delete(previews.lease_key(sibling.id))
    assert previews.reconcile() == 0
    assert previews.get(sibling.id).status is PreviewSessionStatus.Expired
    monkeypatch.setattr(type(services.containers), "stop", original_stop)
    assert previews.reconcile() == 2
    assert services.containers.get(bound_container_id).status is ContainerStatus.Stopped
    assert sibling.container_id is not None
    assert services.containers.get(sibling.container_id).status is ContainerStatus.Stopped
