from __future__ import annotations

from worker.event_bridge import (
    WorkerEventHandlingStatus,
    WorkerStreamEventHandler,
)
from worker.events import (
    StopContainerReason,
    WorkerBuildCancelRegistry,
    WorkerStreamEvent,
    WorkerStreamEventKind,
)


def test_worker_stream_event_handler_stops_containers_and_cancels_builds() -> None:
    stopper = _Stopper()
    acknowledger = _Acknowledger()
    cancel_calls: list[str] = []
    build_cancels = WorkerBuildCancelRegistry()
    build_cancels.register("build-1", lambda: cancel_calls.append("build-1"))
    handler = WorkerStreamEventHandler(
        container_stopper=stopper,
        build_cancels=build_cancels,
        acknowledger=acknowledger,
        worker_id="worker-1",
    )

    stopped = handler.handle(
        WorkerStreamEvent(
            event_id="event-7",
            kind=WorkerStreamEventKind.StopContainer,
            container_id="ctr-1",
            force=True,
            reason=StopContainerReason.User,
        )
    )
    cancelled = handler.handle(
        WorkerStreamEvent(
            event_id="event-8",
            kind=WorkerStreamEventKind.StopBuild,
            container_id="build-1",
        )
    )
    ignored = handler.handle(WorkerStreamEvent(event_id="__heartbeat__"))

    assert stopped.status is WorkerEventHandlingStatus.StoppedContainer
    assert stopped.ok
    assert stopper.calls == [("ctr-1", True, StopContainerReason.User)]
    assert acknowledger.calls == [("event-7", "worker-1")]
    assert cancelled.status is WorkerEventHandlingStatus.CancelledBuild
    assert cancelled.cancel_result is not None
    assert cancelled.cancel_result.invoked
    assert cancel_calls == ["build-1"]
    assert ignored.status is WorkerEventHandlingStatus.Ignored


def test_build_cancel_before_process_registration_is_not_lost() -> None:
    cancellations = WorkerBuildCancelRegistry()
    cancellations.register_pending("starting-build")
    assert cancellations.cancel("starting-build").invoked
    stopped: list[str] = []
    cancellations.register("starting-build", lambda: stopped.append("starting-build"))
    assert stopped == ["starting-build"]
    cancellations.unregister("starting-build")
    assert not cancellations.cancel("starting-build").invoked


def test_worker_stream_event_handler_does_not_acknowledge_failed_container_stop() -> None:
    acknowledger = _Acknowledger()
    result = WorkerStreamEventHandler(
        container_stopper=_FailingStopper(),
        acknowledger=acknowledger,
        worker_id="worker-1",
    ).handle(
        WorkerStreamEvent(
            event_id="event-failed-stop",
            kind=WorkerStreamEventKind.StopContainer,
            container_id="ctr-1",
        )
    )

    assert result.status is WorkerEventHandlingStatus.Error
    assert acknowledger.calls == []


class _Stopper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, StopContainerReason]] = []

    def stop_container(
        self,
        container_id: str,
        *,
        force: bool,
        reason: StopContainerReason = StopContainerReason.Unknown,
    ) -> None:
        self.calls.append((container_id, force, reason))


class _FailingStopper:
    def stop_container(
        self,
        container_id: str,
        *,
        force: bool,
        reason: StopContainerReason = StopContainerReason.Unknown,
    ) -> None:
        _ = (container_id, force, reason)
        raise RuntimeError("stop failed")


class _Acknowledger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def acknowledge_worker_event(self, event_id: str, worker_id: str) -> None:
        self.calls.append((event_id, worker_id))
