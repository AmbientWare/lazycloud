from __future__ import annotations

from api.server.services import ApiServices
from images.building import (
    IMAGE_BUILD_CONTAINER_TTL_SECONDS,
    ImageBuildLifecycleAction,
    plan_image_build_session,
)
from images.lifecycle import (
    ImageBuildContainerLifecycleService,
    ImageBuildContainerLifecycleStatus,
    ImageBuildStopEventResult,
)
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import BuildStatus


def test_image_build_container_lifecycle_service_refreshes_ttl_and_cancels() -> None:
    ttl = _FakeTtlStore()
    stop = _FakeStopPublisher()
    state = _FakeStateStore()
    killer = _FakeKiller()
    service = ImageBuildContainerLifecycleService(ttl, stop, state, killer)
    session = plan_image_build_session(
        ImageSpec(packages=["httpx"]),
        image_id="image-1",
        build_id="build-1",
    )

    start = service.start_session(session)
    cancel = service.cancel_container(
        session.container_id,
        context_cancelled=True,
        build_succeeded=False,
        container_connected=True,
        stopping_ttl_seconds=IMAGE_BUILD_CONTAINER_TTL_SECONDS,
    )

    assert start.status is ImageBuildContainerLifecycleStatus.Complete
    assert ttl.sets == [("build-1", IMAGE_BUILD_CONTAINER_TTL_SECONDS)]
    assert start.cache_metadata["build_container_id"] == "build-1"
    assert start.cache_metadata["build_container_ttl_status"] == "complete"
    assert cancel.status is ImageBuildContainerLifecycleStatus.Complete
    assert cancel.actions == [
        ImageBuildLifecycleAction.SendStopEvent,
        ImageBuildLifecycleAction.MarkStopping,
        ImageBuildLifecycleAction.KillContainer,
    ]
    assert stop.sent == ["build-1"]
    assert state.marked == [("build-1", IMAGE_BUILD_CONTAINER_TTL_SECONDS)]
    assert killer.killed == ["build-1"]
    assert cancel.cache_metadata["build_container_cancel_status"] == "complete"


def test_image_build_container_lifecycle_deletes_pending_state_on_pending_cancel() -> None:
    state = _FakeStateStore()
    service = ImageBuildContainerLifecycleService(
        stop_publisher=_FakeStopPublisher(),
        state_store=state,
    )

    cancel = service.cancel_container(
        "build-container-2",
        context_cancelled=True,
        build_succeeded=False,
        container_connected=False,
        stopping_ttl_seconds=IMAGE_BUILD_CONTAINER_TTL_SECONDS,
    )

    assert cancel.status is ImageBuildContainerLifecycleStatus.Complete
    assert cancel.actions == [
        ImageBuildLifecycleAction.SendStopEvent,
        ImageBuildLifecycleAction.DeletePendingState,
    ]
    assert state.deleted == ["build-container-2"]


def test_runtime_image_build_fails_when_lifecycle_start_errors(
    isolated_services: ApiServices,
) -> None:
    isolated_services.images.container_lifecycle = ImageBuildContainerLifecycleService(
        ttl_store=_FailingTtlStore(),
    )

    record = isolated_services.images.build(ImageSpec(packages=["httpx"]))

    assert record.status is BuildStatus.Failed
    assert record.error == "ttl unavailable"
    assert record.cache_metadata["build_container_ttl_status"] == "error"


class _FakeTtlStore:
    def __init__(self) -> None:
        self.sets: list[tuple[str, int]] = []

    def set_build_container_ttl(self, container_id: str, ttl_seconds: int) -> bool:
        self.sets.append((container_id, ttl_seconds))
        return True


class _FailingTtlStore:
    def set_build_container_ttl(self, container_id: str, ttl_seconds: int) -> bool:
        del container_id, ttl_seconds
        raise RuntimeError("ttl unavailable")


class _FakeStopPublisher:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_stop_build(self, container_id: str) -> ImageBuildStopEventResult:
        self.sent.append(container_id)
        return ImageBuildStopEventResult(
            status=ImageBuildContainerLifecycleStatus.Complete,
            container_id=container_id,
            event_ids=[f"typed-{container_id}"],
            reason="sent",
        )


class _FakeStateStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.marked: list[tuple[str, int]] = []

    def delete_pending_build_container(self, container_id: str) -> bool:
        self.deleted.append(container_id)
        return True

    def mark_build_container_stopping(self, container_id: str, ttl_seconds: int) -> bool:
        self.marked.append((container_id, ttl_seconds))
        return True


class _FakeKiller:
    def __init__(self) -> None:
        self.killed: list[str] = []

    def kill_build_container(self, container_id: str) -> bool:
        self.killed.append(container_id)
        return True
