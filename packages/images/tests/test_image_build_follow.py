from __future__ import annotations

import time

from api.server.services import ApiServices
from database.repositories.image_build_logs import ImageBuildLogRepository
from images.control import stream_build_events
from shared.image_building.authoring import ImageSpec


def test_build_follow_fans_out_progress_and_replays_terminal_logs(
    isolated_services: ApiServices,
) -> None:
    images = isolated_services.images
    workspace_id = isolated_services.control_plane_service.get_workspace().id
    build = images.build(
        ImageSpec(ignore_python=True, commands=["true"]), workspace_id=workspace_id
    )
    assert build.execution_container_id is not None
    first = stream_build_events(images, build.id, workspace_id=workspace_id)
    second = stream_build_events(images, build.id, workspace_id=workspace_id)
    try:
        assert next(first).response.build_id == build.id
        assert next(second).response.build_id == build.id
        images.record_worker_progress(
            build.id,
            workspace_id=workspace_id,
            container_id=build.execution_container_id,
            after=0,
            messages=["first line", "second line"],
        )
        notified_at = time.monotonic()
        for stream in (first, second):
            assert [next(stream).sequence, next(stream).sequence] == [1, 2]
        assert time.monotonic() - notified_at < 3
        images.cancel(build.id, workspace_id=workspace_id)
        for stream in (first, second):
            remaining = list(stream)
            assert remaining[-1].response.done
            assert not remaining[-1].response.success
        replay = list(stream_build_events(images, build.id, workspace_id=workspace_id, after=1))
        assert replay[0].sequence == 2
        assert replay[0].response.msg == "second line\n"
        assert replay[-1].response.done
    finally:
        first.close()
        second.close()


def test_build_follow_recovers_logs_committed_without_a_notification(
    isolated_services: ApiServices,
) -> None:
    images = isolated_services.images
    workspace_id = isolated_services.control_plane_service.get_workspace().id
    build = images.build(
        ImageSpec(ignore_python=True, commands=["true"]), workspace_id=workspace_id
    )
    stream = stream_build_events(images, build.id, workspace_id=workspace_id)
    try:
        next(stream)
        with isolated_services.context.database.session() as session:
            ImageBuildLogRepository(session).append(
                build.id, workspace_id=workspace_id, after=0, messages=["committed without signal"]
            )
        event = next(stream)
        assert event.sequence == 1
        assert event.response.msg == "committed without signal\n"
        images.cancel(build.id, workspace_id=workspace_id)
        assert list(stream)[-1].response.done
    finally:
        stream.close()
