from __future__ import annotations

from api.server.services import ApiServices
from database.repositories.image_build_dispatch import ImageBuildDispatchRepository
from scheduler.containers import SchedulerContainerDispatchStatus
from shared.containers import ContainerStatus
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import BuildStatus
from shared.timestamps import utc_now


def test_scheduling_failure_finishes_image_build_stream_and_cleans_execution(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    scheduler = services.scheduler_container_requests
    scheduler.max_retry_count = 0
    scheduler.retry_grace_seconds = 0
    build = services.images.build(
        ImageSpec(base="scratch", ignore_python=True), workspace_id=workspace_id
    )
    [failure] = scheduler.dispatch_ready(limit=1)

    assert failure.status is SchedulerContainerDispatchStatus.Failed
    failed = services.images.get(build.id, workspace_id=workspace_id)
    assert failed.status is BuildStatus.Failed
    assert failed.error == failure.reason
    assert failed.started_at is None
    assert failed.finished_at is not None
    assert services.containers.get(build.id).status is ContainerStatus.Failed
    events = list(services.image_service.follow_build(build.id, workspace_id=workspace_id))
    assert events[-1].response.done
    assert not events[-1].response.success
    assert events[-1].response.error == failure.reason
    with services.context.database.session() as session:
        dispatch = ImageBuildDispatchRepository(session)
        assert dispatch.payload(build.id, workspace_id=workspace_id) is None
        assert dispatch.cleanup_due(now=utc_now(), limit=1) == [(build.id, workspace_id)]
    services.images.submission.cleanup(build_id=build.id, limit=1)
    with services.context.database.session() as session:
        assert ImageBuildDispatchRepository(session).cleanup_due(now=utc_now(), limit=1) == []
