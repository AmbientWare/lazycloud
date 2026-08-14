from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import FrameType
from typing import Never

import pytest
from api.server.services import ApiServices
from compute.reclaim import ComputeReclaimPolicy
from container_worker_app import main as container_worker
from container_worker_app import runtime as worker_runtime
from coordination.redis_client import RedisClient
from images.settings import ImageBuildContainerSettings
from observability.settings import (
    VolumeMeteringSettings,
)
from scheduler.containers import SchedulerContainerRequestService
from scheduler_app import main as scheduler
from scheduler_app.runtime import SchedulerRuntime
from scheduler_app.services import (
    SchedulerAppServices,
    SchedulerCapacitySettings,
    SchedulerNetworkSettings,
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)
from shared.checkpoints import checkpoint_recent_stub_key
from shared.container_requests import StopContainerReason
from shared.containers import ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.scheduling import WorkerUnavailableReason
from storage.image_archive import ImageArchiveSettings
from storage.retention_settings import RetentionSettings
from tests.real_redis import RealRedisActors
from worker.event_bridge import WorkerEventHandlingResult, WorkerEventHandlingStatus
from worker.events import WorkerStreamEvent, WorkerStreamEventKind
from worker.repository_payloads import StreamWorkerEventsRequest
from worker.scheduler_requests import (
    WorkerSchedulerRequestAction,
    WorkerSchedulerRequestResult,
    WorkerSchedulerRequestStatus,
)
from worker.status import plan_worker_spindown
from worker.worker_lifecycle import (
    WorkerLifecycleAction,
    WorkerLifecycleStepResult,
    WorkerShutdownResult,
)

type _SignalHandler = signal.Handlers | Callable[[int, FrameType | None], None]


def _create_scheduler_app_services(
    services: ApiServices,
    redis: RedisClient,
) -> SchedulerAppServices:
    return SchedulerAppServices.create(
        services.context.database,
        root=services.context.paths.root,
        create_schema=False,
        redis_client=redis,
        gateway_origin=services.gateway_settings.public_http_url,
        runtime_callback_origin=services.gateway_settings.runtime_callback_http_url,
        observability=SchedulerObservabilitySettings(
            workspace_changes=services.workspace_change_stream_settings,
        ),
        storage=SchedulerStorageSettings(
            object_store=services.object_store_settings,
            image_archive=ImageArchiveSettings(),
            retention=RetentionSettings(),
            volume_metering=VolumeMeteringSettings(),
        ),
        network=SchedulerNetworkSettings(
            tailnet_runtime=services.tailnet_runtime_settings,
            tailnet_control=services.tailnet_control_settings,
            backend_routes=services.backend_route_settings,
        ),
        capacity=SchedulerCapacitySettings(
            aws_connections=services.aws_account_connection_settings,
            aws_capacity=services.aws_capacity_settings,
            agent_binaries=services.agent_binary_settings,
            reclaim=ComputeReclaimPolicy(),
        ),
    )


def test_scheduler_runtime_closes_owned_services_on_exception(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    app_services = _create_scheduler_app_services(isolated_services, redis)
    container_requests = app_services.containers.scheduler
    assert isinstance(container_requests, SchedulerContainerRequestService)
    retention = app_services.retention
    assert retention is not None
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="checkpoint-retention-active-deployment",
            kind=DeploymentKind.Endpoint,
        )
    )
    resource = retention.deployment_resources.list(
        workspace=None,
        deployment_id=deployment.id,
        active=True,
    )[0]
    assert (
        checkpoint_recent_stub_key(
            resource.stub.workspace_id,
            resource.stub.id,
        )
        in retention.protected_checkpoint_stub_keys()
    )
    runtime = SchedulerRuntime.from_services(
        scheduler_services=app_services,
        execution_services=app_services,
        runtime_callback_http_url=isolated_services.gateway_settings.runtime_callback_http_url,
        redis_client=redis,
        container_requests=container_requests,
        image_build_container_settings=ImageBuildContainerSettings(),
        retention_settings=RetentionSettings(),
        volume_metering=app_services.volume_metering,
        meter_outbox=app_services.meter_outbox,
        retention=app_services.retention,
        tailnet_cleanup=app_services.tailnet_cleanup,
        custom_domains=app_services.custom_domains,
    )
    runtime.owned_services = app_services
    close_calls: list[SchedulerAppServices] = []
    original_close = SchedulerAppServices.close

    def record_close(services: SchedulerAppServices) -> None:
        close_calls.append(services)
        original_close(services)

    def fail_run_once(
        *,
        now: datetime | None = None,
        include_cron_jobs: bool = True,
        include_containers: bool = True,
        include_container_dispatch: bool = True,
        container_limit: int = 100,
    ) -> Never:
        _ = (
            now,
            include_cron_jobs,
            include_containers,
            include_container_dispatch,
            container_limit,
        )
        raise RuntimeError("scheduler pass failed")

    monkeypatch.setattr(SchedulerAppServices, "close", record_close)
    monkeypatch.setattr(runtime.scheduler, "run_once", fail_run_once)

    with pytest.raises(RuntimeError, match="scheduler pass failed"):
        scheduler.run_scheduler(runtime=runtime, once=True)

    assert close_calls == [app_services]
    runtime.close()
    assert close_calls == [app_services]


def test_scheduler_app_services_stop_cancels_the_real_scheduler_request(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    app_services = _create_scheduler_app_services(isolated_services, redis)
    container_scheduler = app_services.containers.scheduler
    assert isinstance(container_scheduler, SchedulerContainerRequestService)

    container = app_services.containers.run(
        "stale-sandbox",
        "python:3.12-slim",
        ["sleep", "30"],
        docker_enabled=True,
    )

    stopped = app_services.containers.stop(container.id)

    assert stopped.status is ContainerStatus.Stopped
    assert container_scheduler.containers.is_container_cancelled(container.id)
    app_services.close()


def test_container_worker_process_deregisters_on_shutdown_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[int, _SignalHandler] = {}

    def request_shutdown(_call: int) -> None:
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)

    services = _ContainerWorkerServices(
        _ContainerWorkerProcessor(
            WorkerSchedulerRequestResult(
                worker_id="worker-1",
                status=WorkerSchedulerRequestStatus.Idle,
                action=WorkerSchedulerRequestAction.Idle,
            ),
            on_run=request_shutdown,
        )
    )
    restored: list[tuple[int, _SignalHandler]] = []

    def capture_signal(signum: int, handler: _SignalHandler) -> signal.Handlers:
        handlers[signum] = handler
        restored.append((signum, handler))
        return signal.SIG_DFL

    monkeypatch.setattr(container_worker.signal, "signal", capture_signal)

    result = container_worker.run_container_worker(
        settings=container_worker.WorkerSettings.model_validate({"worker_id": "worker-1"}),
        services=services,
        interval_seconds=0,
        keepalive_interval_seconds=15,
    )

    assert result is None
    assert services.processor.calls == 1
    # A signal is an ordinary administrative stop — `docker stop`, systemd, a
    # deploy. Preemption is reported by the provider's capacity reclaim path, so
    # inferring it from SIGTERM would label every restart a preemption.
    assert services.lifecycle.calls == [
        "register_available",
        "keepalive",
        "shutdown:True:ADMIN",
    ]
    assert (signal.SIGTERM, signal.SIG_DFL) in restored


def test_container_worker_process_deregisters_when_startup_after_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _ContainerWorkerServices(
        _ContainerWorkerProcessor(
            WorkerSchedulerRequestResult(
                worker_id="worker-1",
                status=WorkerSchedulerRequestStatus.Idle,
                action=WorkerSchedulerRequestAction.Idle,
            )
        )
    )

    def fail_start_event_loop(
        _services: worker_runtime.ContainerWorkerServices,
        *,
        interval_seconds: float,
        stop_event: threading.Event | None = None,
    ) -> Never:
        _ = interval_seconds, stop_event
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(container_worker, "_start_worker_event_loop", fail_start_event_loop)

    with pytest.raises(RuntimeError, match="event loop unavailable"):
        container_worker.run_container_worker(
            settings=container_worker.WorkerSettings.model_validate({"worker_id": "worker-1"}),
            services=services,
        )

    assert services.processor.calls == 0
    assert services.lifecycle.calls == [
        "register_available",
        "shutdown:True:UNKNOWN",
    ]


def test_container_worker_process_spins_down_idle_nonpersistent_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _ContainerWorkerServices(
        _ContainerWorkerProcessor(
            WorkerSchedulerRequestResult(
                worker_id="worker-1",
                status=WorkerSchedulerRequestStatus.Idle,
                action=WorkerSchedulerRequestAction.Idle,
            )
        )
    )
    monkeypatch.setattr(
        container_worker,
        "time",
        _ContainerWorkerClock((100.0, 401.0)),
    )

    result = container_worker.run_container_worker(
        services=services,
        settings=container_worker.WorkerSettings.model_validate(
            {
                "worker_id": "worker-1",
                "worker_spindown_seconds": 300,
                "configuration": {"execution": {"persistent": False}},
            }
        ),
    )

    assert result is None
    assert services.processor.calls == 1
    assert services.lifecycle.calls == [
        "register_available",
        "keepalive",
        "shutdown:True:UNKNOWN",
    ]


class _ContainerWorkerProcessor:
    def __init__(
        self,
        result: WorkerSchedulerRequestResult,
        *,
        on_run: Callable[[int], None] | None = None,
    ) -> None:
        self.result = result
        self.on_run = on_run
        self.calls = 0

    def run_once(self) -> WorkerSchedulerRequestResult:
        self.calls += 1
        if self.on_run is not None:
            self.on_run(self.calls)
        return self.result


class _ContainerWorkerClock:
    def __init__(self, monotonic_values: tuple[float, ...]) -> None:
        self.monotonic_values = iter(monotonic_values)

    def monotonic(self) -> float:
        return next(self.monotonic_values)


class _BlockingContainerWorkerProcessor:
    def __init__(self, unblock: threading.Event) -> None:
        self.unblock = unblock
        self.calls = 0

    def run_once(self) -> WorkerSchedulerRequestResult:
        self.calls += 1
        self.unblock.wait(timeout=2)
        raise KeyboardInterrupt


class _ContainerWorkerServices:
    def __init__(
        self,
        processor: _ContainerWorkerProcessor | _BlockingContainerWorkerProcessor,
        *,
        event_source: _ContainerWorkerEventSource | None = None,
        worker_events: _ContainerWorkerEventHandler | None = None,
    ) -> None:
        self.identity = _ContainerWorkerIdentity(worker_id="worker-1")
        self.processor = processor
        self.lifecycle = _ContainerWorkerLifecycle()
        self.event_source = event_source
        self.worker_events = worker_events
        self.retention = None


class _ContainerWorkerLifecycle:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def register_available(self):
        self.calls.append("register_available")
        return [WorkerLifecycleStepResult(action=WorkerLifecycleAction.MarkAvailable)]

    def keepalive(self) -> WorkerLifecycleStepResult:
        self.calls.append("keepalive")
        return WorkerLifecycleStepResult(action=WorkerLifecycleAction.KeepAlive)

    def spindown_plan(
        self,
        *,
        persistent: bool = False,
        seconds_since_last_request: float = 0.0,
        spindown_seconds: float,
    ):
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
        self.calls.append(f"shutdown:{remove_worker}:{stop_reason.value}")
        return WorkerShutdownResult(worker_id="worker-1")


@dataclass(slots=True)
class _ContainerWorkerIdentity:
    worker_id: str


class _ContainerWorkerEventSource:
    def __init__(self, event: WorkerStreamEvent) -> None:
        self.event = event
        self.requests: list[StreamWorkerEventsRequest] = []

    def stream_worker_events(
        self,
        request: StreamWorkerEventsRequest,
    ) -> list[WorkerStreamEvent]:
        self.requests.append(request)
        if self.event.kind == WorkerStreamEventKind.Heartbeat:
            return []
        event = self.event
        self.event = WorkerStreamEvent(kind=WorkerStreamEventKind.Heartbeat)
        return [event]


class _ContainerWorkerEventHandler:
    def __init__(self, event_handled: threading.Event) -> None:
        self.event_handled = event_handled
        self.events: list[WorkerStreamEvent] = []

    def handle(self, event: WorkerStreamEvent | None) -> WorkerEventHandlingResult:
        if event is not None:
            self.events.append(event)
        self.event_handled.set()
        return WorkerEventHandlingResult(
            status=WorkerEventHandlingStatus.StoppedContainer,
            event_id=event.event_id if event is not None else "",
            container_id=event.container_id if event is not None else "",
        )
