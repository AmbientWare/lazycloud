from __future__ import annotations

import os
import signal
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Never

import pytest
from container_worker_app import main as container_worker
from container_worker_app.main import (
    ContainerWorkerRegistrationError,
    ContainerWorkerShutdownRequested,
    run_container_worker,
)
from container_worker_app.runtime import ContainerWorkerServices
from container_worker_app.settings import WorkerSettings
from shared.container_requests import StopContainerReason
from shared.scheduling import WorkerUnavailableReason
from worker.configuration import WorkerConfiguration, WorkerExecutionConfiguration
from worker.repository_errors import WorkerRepositoryClientError
from worker.scheduler_requests import (
    WorkerSchedulerRequestAction,
    WorkerSchedulerRequestResult,
    WorkerSchedulerRequestStatus,
)
from worker.status import WorkerSpindownPlan, plan_worker_spindown
from worker.worker_lifecycle import (
    DEFAULT_WORKER_KEEPALIVE_TTL_SECONDS,
    WorkerLifecycleAction,
    WorkerLifecycleStatus,
    WorkerLifecycleStepResult,
    WorkerShutdownResult,
)


@pytest.mark.parametrize("interval", [0, DEFAULT_WORKER_KEEPALIVE_TTL_SECONDS / 2])
def test_worker_refuses_intervals_that_cannot_keep_registration_alive(interval: float) -> None:
    lifecycle = _Lifecycle()
    with pytest.raises(ValueError):
        run_container_worker(
            settings=WorkerSettings(container_service_port=0),
            keepalive_interval_seconds=interval,
            services=_Services(processor=_UnexpectedProcessor(), lifecycle=lifecycle),
        )
    assert not lifecycle.registered


def test_worker_stops_when_repository_error_masks_signal_interrupt() -> None:
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    processor = _SignalMaskingProcessor()
    lifecycle = _Lifecycle()
    services = _Services(processor=processor, lifecycle=lifecycle)

    result = run_container_worker(
        settings=WorkerSettings(container_service_port=0),
        interval_seconds=60,
        services=services,
    )

    assert result is None
    assert processor.calls == 1
    assert lifecycle.shutdown_calls == 1
    assert lifecycle.shutdown_remove_worker == [True]
    assert lifecycle.shutdown_reasons == [StopContainerReason.Admin]
    assert {sig: signal.getsignal(sig) for sig in previous_handlers} == previous_handlers


def test_worker_renews_lease_while_pickup_is_blocked(tmp_path: Path) -> None:
    renewed = threading.Event()
    processor = _KeepaliveBlockingProcessor(renewed)
    lifecycle = _Lifecycle(renewed=renewed)
    services = _Services(processor=processor, lifecycle=lifecycle)
    heartbeat_file = tmp_path / "container-worker.heartbeat"

    with pytest.raises(KeyboardInterrupt):
        run_container_worker(
            settings=WorkerSettings(container_service_port=0),
            interval_seconds=0,
            keepalive_interval_seconds=0.01,
            heartbeat_file=heartbeat_file,
            services=services,
        )

    assert processor.calls == 1
    assert lifecycle.keepalive_calls >= 3
    assert lifecycle.events[-1] == "shutdown"
    assert heartbeat_file.is_file()


def test_worker_does_not_process_when_registration_fails() -> None:
    processor = _UnexpectedProcessor()
    lifecycle = _Lifecycle(
        registration_steps=[
            WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.MarkAvailable,
                status=WorkerLifecycleStatus.Error,
                error_message="cache activation failed",
            )
        ]
    )

    with pytest.raises(ContainerWorkerRegistrationError, match="cache activation failed"):
        run_container_worker(
            settings=WorkerSettings(container_service_port=0),
            once=True,
            services=_Services(processor=processor, lifecycle=lifecycle),
        )

    assert processor.calls == 0
    assert lifecycle.keepalive_calls == 0


def test_a_worker_that_never_registered_leaves_no_record_behind() -> None:
    """Registration failure must not leave a candidate the scheduler will try.

    A persistent worker kept its record on this path. The record declares
    capacity and is admitted to the scheduling candidate set, so a container
    could be told to wait for a worker that had already exited. Identity is
    carried by `WORKER_ID`, not by the record, so nothing is lost by removing
    one that never became available.
    """
    processor = _UnexpectedProcessor()
    lifecycle = _Lifecycle(
        registration_steps=[
            WorkerLifecycleStepResult(action=WorkerLifecycleAction.MarkAvailable),
            WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.ValidateReadiness,
                status=WorkerLifecycleStatus.Error,
                error_message="network readiness failed",
            ),
        ]
    )

    with pytest.raises(ContainerWorkerRegistrationError, match="network readiness failed"):
        run_container_worker(
            settings=WorkerSettings(
                container_service_port=0,
                configuration=WorkerConfiguration(
                    execution=WorkerExecutionConfiguration(persistent=True)
                ),
            ),
            once=True,
            services=_Services(processor=processor, lifecycle=lifecycle),
        )

    assert processor.calls == 0
    assert lifecycle.shutdown_remove_worker == [True]


def test_worker_deregisters_when_startup_after_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor = _UnexpectedProcessor()
    lifecycle = _Lifecycle()

    def fail_start_event_loop(
        _services: ContainerWorkerServices,
        *,
        interval_seconds: float,
        stop_event: threading.Event | None = None,
    ) -> Never:
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(container_worker, "_start_worker_event_loop", fail_start_event_loop)
    with pytest.raises(RuntimeError, match="event loop unavailable"):
        run_container_worker(
            settings=WorkerSettings(container_service_port=0),
            services=_Services(processor=processor, lifecycle=lifecycle),
        )

    assert processor.calls == 0
    assert not lifecycle.registered
    assert lifecycle.shutdown_remove_worker == [True]


def test_idle_nonpersistent_worker_deregisters_after_its_spindown_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _Lifecycle()
    processor = _IdleProcessor()
    monkeypatch.setattr(container_worker, "time", _Clock(iter((100.0, 401.0))))

    run_container_worker(
        settings=WorkerSettings(
            container_service_port=0,
            worker_spindown_seconds=300,
            configuration=WorkerConfiguration(
                execution=WorkerExecutionConfiguration(persistent=False)
            ),
        ),
        services=_Services(processor=processor, lifecycle=lifecycle),
    )

    assert processor.calls == 1
    assert not lifecycle.registered
    assert lifecycle.shutdown_remove_worker == [True]


@dataclass(slots=True)
class _Clock:
    ticks: Iterator[float]

    def monotonic(self) -> float:
        return next(self.ticks)


@dataclass(slots=True)
class _IdleProcessor:
    calls: int = 0

    def run_once(self) -> WorkerSchedulerRequestResult:
        self.calls += 1
        return WorkerSchedulerRequestResult(
            worker_id="worker-1",
            status=WorkerSchedulerRequestStatus.Idle,
            action=WorkerSchedulerRequestAction.Idle,
        )


@dataclass(slots=True)
class _SignalMaskingProcessor:
    calls: int = 0

    def run_once(self) -> Never:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("worker retried after shutdown was requested")
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except ContainerWorkerShutdownRequested as exc:
            raise WorkerRepositoryClientError("control plane unavailable") from exc
        raise AssertionError("SIGTERM handler did not interrupt the repository call")


@dataclass(slots=True)
class _KeepaliveBlockingProcessor:
    renewed: threading.Event
    calls: int = 0

    def run_once(self) -> Never:
        self.calls += 1
        if not self.renewed.wait(timeout=2):
            raise AssertionError("worker pickup blocked lease renewal")
        raise KeyboardInterrupt


@dataclass(slots=True)
class _UnexpectedProcessor:
    calls: int = 0

    def run_once(self) -> Never:
        self.calls += 1
        raise AssertionError("worker processed a request before registration succeeded")


@dataclass(slots=True)
class _Lifecycle:
    registered: bool = False
    shutdown_calls: int = 0
    shutdown_remove_worker: list[bool] = field(default_factory=list)
    shutdown_reasons: list[StopContainerReason] = field(default_factory=list)
    shutdown_unavailable: list[tuple[WorkerUnavailableReason, str]] = field(default_factory=list)
    keepalive_calls: int = 0
    renewed: threading.Event | None = None
    events: list[str] = field(default_factory=list)
    registration_steps: list[WorkerLifecycleStepResult] = field(
        default_factory=lambda: [
            WorkerLifecycleStepResult(action=WorkerLifecycleAction.MarkAvailable)
        ]
    )

    def register_available(self) -> list[WorkerLifecycleStepResult]:
        self.registered = True
        return self.registration_steps

    def keepalive(self) -> WorkerLifecycleStepResult:
        self.keepalive_calls += 1
        self.events.append("keepalive")
        if self.renewed is not None and self.keepalive_calls >= 3:
            self.renewed.set()
        return WorkerLifecycleStepResult(action=WorkerLifecycleAction.KeepAlive)

    def spindown_plan(
        self,
        *,
        persistent: bool = False,
        seconds_since_last_request: float = 0.0,
        spindown_seconds: float,
    ) -> WorkerSpindownPlan:
        return plan_worker_spindown(
            persistent=persistent,
            seconds_since_last_request=seconds_since_last_request,
            active_container_count=0,
            spindown_seconds=spindown_seconds,
        )

    def shutdown(
        self,
        *,
        remove_worker: bool = True,
        stop_reason: StopContainerReason = StopContainerReason.Unknown,
        unavailable_reason: WorkerUnavailableReason = WorkerUnavailableReason.ShuttingDown,
        unavailable_detail: str = "",
    ) -> WorkerShutdownResult:
        self.shutdown_calls += 1
        if remove_worker:
            self.registered = False
        self.shutdown_remove_worker.append(remove_worker)
        self.shutdown_reasons.append(stop_reason)
        self.shutdown_unavailable.append((unavailable_reason, unavailable_detail))
        self.events.append("shutdown")
        return WorkerShutdownResult(worker_id="worker-1")


@dataclass(slots=True)
class _Services:
    processor: (
        _SignalMaskingProcessor
        | _KeepaliveBlockingProcessor
        | _UnexpectedProcessor
        | _IdleProcessor
    )
    lifecycle: _Lifecycle
    event_source: None = None
    worker_events: None = None
    retention: None = None
    memory_watcher: None = None
    credential_refresher: None = None
