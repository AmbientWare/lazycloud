from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Never

import pytest
from container_worker_app.main import (
    DEFAULT_WORKER_KEEPALIVE_INTERVAL_SECONDS,
    ContainerWorkerRegistrationError,
    ContainerWorkerShutdownRequested,
    _container_worker_shutdown_handlers,
    _parse_arguments,
    _validate_keepalive_interval,
    run_container_worker,
)
from container_worker_app.production import ProductionWorkerSettings
from shared.container_requests import StopContainerReason
from worker.repository_client import WorkerRepositoryClientError
from worker.status import WorkerSpindownPlan
from worker.worker_lifecycle import (
    WorkerLifecycleAction,
    WorkerLifecycleStatus,
    WorkerLifecycleStepResult,
    WorkerShutdownResult,
)


def test_shutdown_handler_records_request_before_interrupting_worker() -> None:
    shutdown_event = threading.Event()

    with (
        pytest.raises(ContainerWorkerShutdownRequested),
        _container_worker_shutdown_handlers(shutdown_event),
    ):
        os.kill(os.getpid(), signal.SIGTERM)

    assert shutdown_event.is_set()


def test_worker_keepalive_defaults_and_bounds_are_lease_safe() -> None:
    assert (
        _parse_arguments([]).keepalive_interval_seconds == DEFAULT_WORKER_KEEPALIVE_INTERVAL_SECONDS
    )
    _validate_keepalive_interval(20)
    with pytest.raises(ValueError, match="no more than 20 seconds"):
        _validate_keepalive_interval(20.1)
    with pytest.raises(ValueError, match="greater than zero"):
        _validate_keepalive_interval(0)


def test_worker_stops_when_repository_error_masks_signal_interrupt() -> None:
    processor = _SignalMaskingProcessor()
    lifecycle = _Lifecycle()
    services = _Services(processor=processor, lifecycle=lifecycle)

    result = run_container_worker(
        settings=ProductionWorkerSettings(container_service_port=0),
        interval_seconds=60,
        services=services,
    )

    assert result is None
    assert processor.calls == 1
    assert lifecycle.shutdown_calls == 1
    assert lifecycle.shutdown_remove_worker == [True]
    assert lifecycle.shutdown_reasons == [StopContainerReason.Preempted]


def test_worker_renews_lease_while_pickup_is_blocked(tmp_path: Path) -> None:
    renewed = threading.Event()
    processor = _KeepaliveBlockingProcessor(renewed)
    lifecycle = _Lifecycle(renewed=renewed)
    services = _Services(processor=processor, lifecycle=lifecycle)
    heartbeat_file = tmp_path / "container-worker.heartbeat"

    with pytest.raises(KeyboardInterrupt):
        run_container_worker(
            settings=ProductionWorkerSettings(container_service_port=0),
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
            settings=ProductionWorkerSettings(container_service_port=0),
            once=True,
            services=_Services(processor=processor, lifecycle=lifecycle),
        )

    assert processor.calls == 0
    assert lifecycle.keepalive_calls == 0


def test_persistent_worker_registration_rollback_preserves_owner_identity() -> None:
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
            settings=ProductionWorkerSettings(container_service_port=0, persistent=True),
            once=True,
            services=_Services(processor=processor, lifecycle=lifecycle),
        )

    assert processor.calls == 0
    assert lifecycle.shutdown_remove_worker == [False]


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
    shutdown_calls: int = 0
    shutdown_remove_worker: list[bool] = field(default_factory=list)
    shutdown_reasons: list[StopContainerReason] = field(default_factory=list)
    keepalive_calls: int = 0
    renewed: threading.Event | None = None
    events: list[str] = field(default_factory=list)
    registration_steps: list[WorkerLifecycleStepResult] = field(
        default_factory=lambda: [
            WorkerLifecycleStepResult(action=WorkerLifecycleAction.MarkAvailable)
        ]
    )

    def register_available(self) -> list[WorkerLifecycleStepResult]:
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
        del persistent, seconds_since_last_request, spindown_seconds
        raise AssertionError("spindown should not run after shutdown is requested")

    def shutdown(
        self,
        *,
        remove_worker: bool = True,
        stop_reason: StopContainerReason = StopContainerReason.Unknown,
    ) -> WorkerShutdownResult:
        self.shutdown_calls += 1
        self.shutdown_remove_worker.append(remove_worker)
        self.shutdown_reasons.append(stop_reason)
        self.events.append("shutdown")
        return WorkerShutdownResult(worker_id="worker-1")


@dataclass(slots=True)
class _Services:
    processor: _SignalMaskingProcessor | _KeepaliveBlockingProcessor | _UnexpectedProcessor
    lifecycle: _Lifecycle
    event_source: None = None
    worker_events: None = None
    artifact_retention: None = None
