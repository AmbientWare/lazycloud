from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import JsonValue
from scheduler.fleet import SchedulerContainerStatus
from scheduler.state import (
    DEFAULT_CONTAINER_STATE_TTL_SECONDS,
    ContainerStatusUpdatePlan,
    SchedulerContainerState,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    WorkerCapacityChange,
    WorkerCapacityPlan,
)
from shared.compute_policy import MachinePool
from shared.container_requests import StopContainerReason, WorkerStartupKind
from worker.container_execution import (
    ContainerExecutionContext,
    ContainerExecutionPhase,
    ContainerExecutionPhaseResult,
    ContainerExecutionResult,
)
from worker.events import (
    ContainerRequestContext,
    WorkerPoolMode,
    WorkerUsageEvidence,
    WorkerUsageMetricName,
    WorkerUsageMetricPlan,
)
from worker.image_build_execution import (
    WorkerImageBuildExecutionResult,
    WorkerImageBuildStatus,
)
from worker.scheduler_requests import (
    WorkerSchedulerRequestAction,
    WorkerSchedulerRequestProcessor,
    WorkerSchedulerRequestResult,
    WorkerSchedulerRequestStatus,
    container_execution_context_from_scheduler_request,
)
from worker.supervision import WorkerUsageEmissionResult
from worker.worker_lifecycle import WorkerLifecycleOrchestrator

_CAPACITY_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def test_worker_scheduler_request_processor_executes_and_releases_capacity() -> None:
    request = _request(payload={"image_id": "image-1", "startup_kind": "function"})
    workers = _WorkerRepository(requests=[request])
    containers = _ContainerRepository(
        states={
            "ctr-1": _state(
                request,
                status=SchedulerContainerStatus.Pending,
            )
        }
    )
    execution = _ExecutionService()
    processor = WorkerSchedulerRequestProcessor(
        worker_id="worker-1",
        workers=workers,
        containers=containers,
        execution=execution,
        worker_gpu_type="",
    )

    started = processor.run_once()

    assert started.status is WorkerSchedulerRequestStatus.Executed
    assert started.action is WorkerSchedulerRequestAction.Execute
    assert started.processed
    assert started.background
    assert not started.capacity_released

    result = _wait_for_background_result(processor, "ctr-1")

    assert result.status is WorkerSchedulerRequestStatus.Executed
    assert result.capacity_released
    assert workers.capacity_changes == [("worker-1", "ctr-1", WorkerCapacityChange.Add)]
    assert execution.contexts[0].request.image_id == "image-1"
    assert execution.contexts[0].startup_kind is WorkerStartupKind.Function
    assert execution.contexts[0].run_delayed_cleanup


def test_worker_scheduler_request_processor_executes_image_build_branch() -> None:
    request = _request(
        payload={
            "kind": "image-build",
            "build_id": "build-1",
            "image_id": "image-1",
            "build_options": {"dockerfile": "FROM python:3.12-slim\n"},
        }
    )
    workers = _WorkerRepository(requests=[request])
    execution = _ExecutionService()
    image_builds = _ImageBuildExecutionService()
    containers = _ContainerRepository(
        states={"ctr-1": _state(request, status=SchedulerContainerStatus.Pending)}
    )
    processor = WorkerSchedulerRequestProcessor(
        worker_id="worker-1",
        workers=workers,
        containers=containers,
        execution=execution,
        worker_gpu_type="",
        image_builds=image_builds,
        usage_recorder=_UsageWindowRecorder(),
    )

    result = processor.run_once()

    assert result.status is WorkerSchedulerRequestStatus.Executed
    assert result.action is WorkerSchedulerRequestAction.Execute
    assert result.capacity_released
    assert result.image_build is not None
    assert result.image_build.object_key == "image-1.rclip"
    assert image_builds.requests == [request]
    assert execution.contexts == []
    assert containers.status_updates == [
        ("ctr-1", SchedulerContainerStatus.Running),
        ("ctr-1", SchedulerContainerStatus.Complete),
    ]
    assert containers.exit_codes == [("ctr-1", 0)]
    assert containers.ttls == [
        DEFAULT_CONTAINER_STATE_TTL_SECONDS,
        DEFAULT_CONTAINER_STATE_TTL_SECONDS,
    ]


def test_worker_scheduler_request_processor_bills_an_image_build_that_failed() -> None:
    """A build that fails held the capacity it was placed with and is billed for it.

    The window carries the placed cpu, memory and card, which is what the ledger
    prices the build's recorded placement against.
    """

    request = _request(
        payload={
            "kind": "image-build",
            "build_id": "build-1",
            "image_id": "image-1",
            "build_options": {"dockerfile": "FROM python:3.12-slim\n"},
        },
        gpu_type="A100",
        gpu_count=1,
    )
    workers = _WorkerRepository(requests=[request])
    containers = _ContainerRepository(
        states={"ctr-1": _state(request, status=SchedulerContainerStatus.Pending)}
    )
    usage = _UsageWindowRecorder()
    processor = WorkerSchedulerRequestProcessor(
        worker_id="worker-1",
        workers=workers,
        containers=containers,
        execution=_ExecutionService(),
        worker_gpu_type="A100",
        image_builds=_FailingImageBuildExecutionService(),
        usage_recorder=usage,
    )

    result = processor.run_once()

    assert result.status is WorkerSchedulerRequestStatus.Error
    assert containers.exit_codes == [("ctr-1", 1)]
    [window] = usage.windows
    assert window.container_id == "ctr-1"
    assert window.duration_ms > 0
    assert window.window_start_ms == 0
    assert window.window_end_ms == window.duration_ms
    assert (
        window.metering_window_ended_at - window.metering_window_started_at
    ).total_seconds() * 1000 == window.duration_ms
    [plan] = window.plans
    assert plan.labels["cpu_millicores"] == 1000
    assert plan.labels["mem_mb"] == 512
    assert plan.labels["gpu"] == "A100"
    assert plan.labels["gpu_count"] == 1


def test_worker_scheduler_request_processor_tracks_active_container_for_shutdown() -> None:
    request = _request(payload={"image_id": "image-1", "startup_kind": "function"})
    workers = _WorkerRepository(requests=[request])
    containers = _ContainerRepository(
        states={"ctr-1": _state(request, status=SchedulerContainerStatus.Pending)}
    )
    runtime_started = threading.Event()
    stop_requested = threading.Event()
    stopper = _ShutdownStopper(stop_requested)
    lifecycle = WorkerLifecycleOrchestrator(worker_id="worker-1", stopper=stopper)
    execution = _BlockingExecutionService(
        started=runtime_started,
        stop_requested=stop_requested,
        containers=containers,
    )
    processor = WorkerSchedulerRequestProcessor(
        worker_id="worker-1",
        workers=workers,
        containers=containers,
        execution=execution,
        worker_gpu_type="",
        lifecycle=lifecycle,
    )
    started = processor.run_once()

    assert started.background
    assert runtime_started.wait(timeout=1)
    assert lifecycle.active_container_ids() == ["ctr-1"]

    shutdown = lifecycle.shutdown(
        drain_timeout_seconds=0,
        stop_grace_seconds=1,
        force_stop_wait_seconds=0,
        remove_worker=False,
    )
    result = _wait_for_background_result(processor, "ctr-1")

    assert shutdown.ok
    assert stopper.stopped == [("ctr-1", False)]
    assert lifecycle.active_container_ids() == []
    assert containers.deleted == ["ctr-1"]
    assert workers.capacity_changes == [("worker-1", "ctr-1", WorkerCapacityChange.Add)]
    assert result.status is WorkerSchedulerRequestStatus.Executed


def test_worker_scheduler_request_processor_backgrounds_long_lived_container() -> None:
    request = _request(payload={"image_id": "image-1", "startup_kind": "function"})
    workers = _WorkerRepository(requests=[request])
    containers = _ContainerRepository(
        states={"ctr-1": _state(request, status=SchedulerContainerStatus.Pending)}
    )
    runtime_started = threading.Event()
    stop_requested = threading.Event()
    lifecycle = WorkerLifecycleOrchestrator(
        worker_id="worker-1",
        stopper=_ShutdownStopper(stop_requested),
    )
    execution = _BlockingExecutionService(
        started=runtime_started,
        stop_requested=stop_requested,
        containers=containers,
    )
    processor = WorkerSchedulerRequestProcessor(
        worker_id="worker-1",
        workers=workers,
        containers=containers,
        execution=execution,
        worker_gpu_type="",
        lifecycle=lifecycle,
    )

    started = processor.run_once()

    assert started.status is WorkerSchedulerRequestStatus.Executed
    assert started.background
    assert not started.capacity_released
    assert runtime_started.wait(timeout=1)
    assert lifecycle.active_container_ids() == ["ctr-1"]

    idle = processor.run_once()
    assert idle.status is WorkerSchedulerRequestStatus.Idle
    assert workers.capacity_changes == []

    stop_requested.set()
    for _ in range(20):
        reaped = processor.run_once()
        if reaped.background and reaped.container_id == "ctr-1":
            break
        threading.Event().wait(0.01)
    else:  # pragma: no cover - indicates the background thread never finished
        raise AssertionError("background execution was not reaped")

    assert reaped.status is WorkerSchedulerRequestStatus.Executed
    assert reaped.background
    assert reaped.capacity_released
    assert lifecycle.active_container_ids() == []
    assert workers.capacity_changes == [("worker-1", "ctr-1", WorkerCapacityChange.Add)]


def test_worker_scheduler_request_processor_drops_missing_state_and_releases_capacity() -> None:
    request = _request()
    workers = _WorkerRepository(requests=[request])
    processor = WorkerSchedulerRequestProcessor(
        worker_id="worker-1",
        workers=workers,
        containers=_ContainerRepository(),
        execution=_ExecutionService(),
        worker_gpu_type="",
    )

    result = processor.run_once()

    assert result.status is WorkerSchedulerRequestStatus.Dropped
    assert result.action is WorkerSchedulerRequestAction.DropMissingState
    assert result.capacity_released
    assert workers.capacity_changes == [("worker-1", "ctr-1", WorkerCapacityChange.Add)]


def test_worker_scheduler_request_processor_drops_stopping_state_and_deletes_state() -> None:
    request = _request()
    containers = _ContainerRepository(
        states={"ctr-1": _state(request, status=SchedulerContainerStatus.Stopping)}
    )
    processor = WorkerSchedulerRequestProcessor(
        worker_id="worker-1",
        workers=_WorkerRepository(requests=[request]),
        containers=containers,
        execution=_ExecutionService(),
        worker_gpu_type="",
    )

    result = processor.run_once()

    assert result.status is WorkerSchedulerRequestStatus.Dropped
    assert result.action is WorkerSchedulerRequestAction.DropStoppingState
    assert result.state_deleted
    assert containers.deleted == ["ctr-1"]


def test_worker_scheduler_request_processor_reports_execution_failure() -> None:
    request = _request(payload={"startup_kind": "function"})
    workers = _WorkerRepository(requests=[request])
    execution_result = ContainerExecutionResult(
        phases=[
            ContainerExecutionPhaseResult(
                phase=ContainerExecutionPhase.LoadImage,
                ok=False,
                error_message="image load failed",
            )
        ]
    )
    processor = WorkerSchedulerRequestProcessor(
        worker_id="worker-1",
        workers=workers,
        containers=_ContainerRepository(
            states={"ctr-1": _state(request, status=SchedulerContainerStatus.Pending)}
        ),
        execution=_ExecutionService(result=execution_result),
        worker_gpu_type="",
    )

    started = processor.run_once()

    assert started.background
    assert not started.capacity_released
    result = _wait_for_background_result(processor, "ctr-1")

    assert result.status is WorkerSchedulerRequestStatus.Error
    assert result.capacity_released
    assert result.error_message == "worker execution failed at load-image"
    assert workers.capacity_changes == [("worker-1", "ctr-1", WorkerCapacityChange.Add)]


def _request(
    *,
    payload: dict[str, JsonValue] | None = None,
    gpu_type: str = "",
    gpu_count: int = 0,
) -> SchedulerWorkerRequest:
    return SchedulerWorkerRequest(
        workspace_id="workspace-1",
        stub_id="stub-1",
        container_id="ctr-1",
        cpu_millicores=1000,
        memory_mib=512,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        payload=payload or {},
    )


def _wait_for_background_result(
    processor: WorkerSchedulerRequestProcessor,
    container_id: str,
) -> WorkerSchedulerRequestResult:
    for _ in range(20):
        result = processor.run_once()
        if result.background and result.container_id == container_id:
            return result
        threading.Event().wait(0.01)
    raise AssertionError("background execution was not reaped")


def _state(
    request: SchedulerWorkerRequest,
    *,
    status: SchedulerContainerStatus,
) -> SchedulerContainerState:
    return SchedulerContainerState(
        container_id=request.container_id,
        stub_id=request.stub_id,
        workspace_id=request.workspace_id,
        worker_id="worker-1",
        status=status,
    )


@dataclass(slots=True)
class _WorkerRepository:
    requests: list[SchedulerWorkerRequest] = field(default_factory=list)
    capacity_changes: list[tuple[str, str, WorkerCapacityChange]] = field(default_factory=list)

    def get_next_container_request(self, worker_id: str) -> SchedulerWorkerRequest | None:
        _ = worker_id
        return self.requests.pop(0) if self.requests else None

    def update_worker_capacity(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
        change: WorkerCapacityChange,
    ) -> WorkerCapacityPlan:
        self.capacity_changes.append((worker_id, request.container_id, change))
        return WorkerCapacityPlan(
            worker=SchedulerWorkerRecord(
                worker_id=worker_id,
                pool=MachinePool("test"),
                capacity_owner_id=_CAPACITY_OWNER_ID,
            ),
            change=change,
            request=request,
            accepted=True,
        )


@dataclass(slots=True)
class _ContainerRepository:
    states: dict[str, SchedulerContainerState] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    status_updates: list[tuple[str, SchedulerContainerStatus]] = field(default_factory=list)
    exit_codes: list[tuple[str, int]] = field(default_factory=list)
    ttls: list[int] = field(default_factory=list)

    def get_container_state(self, container_id: str) -> SchedulerContainerState | None:
        return self.states.get(container_id)

    def update_container_status(
        self,
        container_id: str,
        status: SchedulerContainerStatus,
        *,
        ttl_seconds: int,
    ) -> ContainerStatusUpdatePlan:
        current = self.states[container_id]
        updated = current.model_copy(update={"status": status})
        self.states[container_id] = updated
        self.status_updates.append((container_id, status))
        self.ttls.append(ttl_seconds)
        return ContainerStatusUpdatePlan(
            container_id=container_id,
            previous_status=current.status,
            next_status=status,
            changed=current.status is not status,
            ttl_seconds=ttl_seconds,
        )

    def set_exit_code(
        self,
        container_id: str,
        exit_code: int,
        *,
        termination_reason: StopContainerReason = StopContainerReason.Unknown,
        failed_phase: ContainerExecutionPhase | None = None,
        failure_detail: str = "",
    ) -> None:
        del termination_reason, failed_phase, failure_detail
        self.exit_codes.append((container_id, exit_code))

    def delete_container_state(self, container_id: str) -> bool:
        self.deleted.append(container_id)
        return self.states.pop(container_id, None) is not None


@dataclass(slots=True)
class _ExecutionService:
    result: ContainerExecutionResult = field(default_factory=ContainerExecutionResult)
    contexts: list[ContainerExecutionContext] = field(default_factory=list)

    def execute(self, context: ContainerExecutionContext) -> ContainerExecutionResult:
        self.contexts.append(context)
        return self.result


@dataclass(slots=True)
class _ImageBuildExecutionService:
    requests: list[SchedulerWorkerRequest] = field(default_factory=list)

    def execute(self, request: SchedulerWorkerRequest) -> WorkerImageBuildExecutionResult:
        self.requests.append(request)
        return WorkerImageBuildExecutionResult(
            ok=True,
            container_id=request.container_id,
            image_id=str(request.payload["image_id"]),
            build_id=str(request.payload["build_id"]),
            object_key=f"{request.payload['image_id']}.rclip",
            status=WorkerImageBuildStatus.Complete,
        )


@dataclass(slots=True)
class _FailingImageBuildExecutionService:
    def execute(self, request: SchedulerWorkerRequest) -> WorkerImageBuildExecutionResult:
        return WorkerImageBuildExecutionResult(
            ok=False,
            container_id=request.container_id,
            image_id=str(request.payload["image_id"]),
            build_id=str(request.payload["build_id"]),
            status=WorkerImageBuildStatus.Failed,
            error_message="image archive build failed",
        )


@dataclass(slots=True)
class _UsageWindowRecorder:
    windows: list[WorkerUsageEmissionResult] = field(default_factory=list)
    failure: Exception | None = None

    def record_usage_window(
        self,
        request: ContainerRequestContext,
        *,
        duration_ms: int,
        window_start_ms: int = 0,
        window_end_ms: int | None = None,
        metering_window_started_at: datetime,
        metering_window_ended_at: datetime,
        evidence: WorkerUsageEvidence | None = None,
    ) -> WorkerUsageEmissionResult:
        _ = evidence
        if self.failure is not None:
            raise self.failure
        result = WorkerUsageEmissionResult(
            worker_id="worker-1",
            container_id=request.container_id,
            duration_ms=duration_ms,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms if window_end_ms is not None else duration_ms,
            metering_window_started_at=metering_window_started_at,
            metering_window_ended_at=metering_window_ended_at,
            pool_mode=WorkerPoolMode.Public,
            plans=(
                WorkerUsageMetricPlan(
                    name=WorkerUsageMetricName.ContainerDuration,
                    labels={
                        "container_id": request.container_id,
                        "cpu_millicores": request.cpu_millicores,
                        "mem_mb": request.memory_mib,
                        "gpu": request.gpu,
                        "gpu_count": request.gpu_count,
                    },
                    value=float(duration_ms),
                ),
            ),
        )
        self.windows.append(result)
        return result


@dataclass(slots=True)
class _BlockingExecutionService:
    started: threading.Event
    stop_requested: threading.Event
    containers: _ContainerRepository
    contexts: list[ContainerExecutionContext] = field(default_factory=list)

    def execute(self, context: ContainerExecutionContext) -> ContainerExecutionResult:
        self.contexts.append(context)
        self.started.set()
        assert self.stop_requested.wait(timeout=1)
        self.containers.delete_container_state(context.request.container_id)
        return ContainerExecutionResult()


@dataclass(slots=True)
class _ShutdownStopper:
    stop_requested: threading.Event
    stopped: list[tuple[str, bool]] = field(default_factory=list)

    def stop_container(
        self,
        container_id: str,
        *,
        force: bool,
        reason: StopContainerReason = StopContainerReason.Unknown,
    ) -> None:
        del reason
        self.stopped.append((container_id, force))
        self.stop_requested.set()


def test_a_container_is_billed_for_the_card_it_ran_on() -> None:
    """The usage label names the machine's GPU, not the one the request asked for.

    A request may say `any`, or name a model as a list, and the scheduler resolves
    it against real capacity. `any` is not a rate anything can price, so a
    container billed under it produces an unpriced line for compute that plainly
    ran on a real card.
    """

    context = container_execution_context_from_scheduler_request(
        _request(gpu_type="any", gpu_count=1, payload={"image_id": "img-1"}),
        worker_gpu_type="H100",
    )

    assert context.request.gpu == "H100"
    assert context.request.gpu_count == 1


def test_a_cpu_only_container_on_a_gpu_host_is_billed_for_no_card() -> None:
    """No GPU allocated means no GPU stamped, whatever the host happens to hold.

    GPU seconds are priced per model, so stamping the host's card on a container
    that was never given one invents a charge out of the machine it landed on.
    """

    context = container_execution_context_from_scheduler_request(
        _request(gpu_type="", gpu_count=0, payload={"image_id": "img-1"}),
        worker_gpu_type="H100",
    )

    assert context.request.gpu == ""
    assert context.request.gpu_count == 0


def test_a_worker_that_names_no_particular_card_bills_for_none() -> None:
    """`any` reaches worker configuration verbatim and must not be billed as a model.

    A pool joined with `gpu=["any"]` puts that word ahead of the machine's own
    detected cards, so every worker in it registers `any` as its model. Passing it
    through would price the container at an unknown model on a machine whose real
    card we never confirmed; an empty model is a visible unpriced gap instead.
    """

    context = container_execution_context_from_scheduler_request(
        _request(gpu_type="H100", gpu_count=1, payload={"image_id": "img-1"}),
        worker_gpu_type="any",
    )

    assert context.request.gpu == ""
    assert context.request.gpu_count == 1
