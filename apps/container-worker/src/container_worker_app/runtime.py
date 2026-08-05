from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

from shared.container_requests import StopContainerReason
from shared.scheduling import WorkerUnavailableReason
from worker.event_bridge import WorkerEventHandlingResult
from worker.events import WorkerStreamEvent
from worker.repository_payloads import StreamWorkerEventsRequest
from worker.retention import WorkerRetentionService
from worker.scheduler_requests import WorkerSchedulerRequestResult
from worker.status import WorkerSpindownPlan
from worker.worker_lifecycle import WorkerLifecycleStepResult, WorkerShutdownResult

from container_worker_app.composition import build_worker_process_services
from container_worker_app.settings import WorkerSettings


class ContainerWorkerProcessor(Protocol):
    def run_once(self) -> WorkerSchedulerRequestResult: ...


class ContainerWorkerLifecycle(Protocol):
    def register_available(self) -> list[WorkerLifecycleStepResult]: ...

    def keepalive(self) -> WorkerLifecycleStepResult: ...

    def spindown_plan(
        self,
        *,
        persistent: bool = False,
        seconds_since_last_request: float = 0.0,
        spindown_seconds: float,
    ) -> WorkerSpindownPlan: ...

    def shutdown(
        self,
        *,
        remove_worker: bool = True,
        stop_reason: StopContainerReason = StopContainerReason.Unknown,
        unavailable_reason: WorkerUnavailableReason = WorkerUnavailableReason.ShuttingDown,
        unavailable_detail: str = "",
    ) -> WorkerShutdownResult: ...


class ContainerWorkerEventSource(Protocol):
    def stream_worker_events(
        self,
        request: StreamWorkerEventsRequest,
    ) -> Iterable[WorkerStreamEvent]: ...


class ContainerWorkerEventHandler(Protocol):
    def handle(self, event: WorkerStreamEvent | None) -> WorkerEventHandlingResult: ...


class ContainerWorkerServices(Protocol):
    @property
    def processor(self) -> ContainerWorkerProcessor: ...

    @property
    def lifecycle(self) -> ContainerWorkerLifecycle: ...

    @property
    def event_source(self) -> ContainerWorkerEventSource | None: ...

    @property
    def worker_events(self) -> ContainerWorkerEventHandler | None: ...

    @property
    def retention(self) -> WorkerRetentionService | None: ...


@dataclass(slots=True)
class ContainerWorkerRuntime:
    settings: WorkerSettings
    services: ContainerWorkerServices

    @classmethod
    def production(
        cls,
        *,
        settings: WorkerSettings,
    ) -> ContainerWorkerRuntime:
        return cls(
            settings=settings,
            services=build_worker_process_services(settings=settings),
        )

    @classmethod
    def from_services(
        cls,
        *,
        settings: WorkerSettings,
        services: ContainerWorkerServices,
    ) -> ContainerWorkerRuntime:
        return cls(settings=settings, services=services)

    def close(self) -> None:
        pass

    def __enter__(self) -> ContainerWorkerRuntime:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()
