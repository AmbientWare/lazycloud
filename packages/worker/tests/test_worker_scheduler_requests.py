from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from pydantic import JsonValue
from shared.compute_policy import MachinePool
from shared.container_requests import StopContainerReason, WorkerStartupKind
from shared.scheduling import (
    ContainerStatusUpdatePlan,
    SchedulerContainerStatus,
    WorkerCapacityChange,
    WorkerCapacityResult,
    WorkerContainerState,
    WorkerExecutionRecord,
    WorkerExecutionRequest,
)
from worker.container_execution import (
    ContainerExecutionContext,
    ContainerExecutionPhase,
    ContainerExecutionPhaseResult,
    ContainerExecutionResult,
)
from worker.events import WorkerBuildCancelRegistry
from worker.repository_client import (
    WorkerRepositoryClientError,
)
from worker.scheduler_requests import (
    WorkerSchedulerRequestAction,
    WorkerSchedulerRequestProcessor,
    WorkerSchedulerRequestResult,
    WorkerSchedulerRequestStatus,
    container_execution_context_from_scheduler_request,
)
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
        build_cancels=WorkerBuildCancelRegistry(),
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
        build_cancels=WorkerBuildCancelRegistry(),
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
        build_cancels=WorkerBuildCancelRegistry(),
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
        build_cancels=WorkerBuildCancelRegistry(),
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
        build_cancels=WorkerBuildCancelRegistry(),
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
        build_cancels=WorkerBuildCancelRegistry(),
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


def test_worker_scheduler_request_processor_reconciles_a_redelivered_request() -> None:
    """A redelivery of a container this worker holds must not start a second one."""

    request = _request(payload={"image_id": "image-1", "startup_kind": "function"})
    workers = _WorkerRepository(
        requests=[request],
        acknowledge_error="control plane is unreachable",
    )
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
        build_cancels=WorkerBuildCancelRegistry(),
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

    redelivered = processor.run_once()

    assert redelivered.status is WorkerSchedulerRequestStatus.Reconciled
    assert redelivered.action is WorkerSchedulerRequestAction.ReconcileDelivery
    assert [context.request.container_id for context in execution.contexts] == ["ctr-1"]
    assert lifecycle.active_container_ids() == ["ctr-1"]
    assert workers.capacity_changes == []
    stop_requested.set()


def test_worker_scheduler_request_processor_keeps_a_request_it_could_not_act_on() -> None:
    """An unreachable control plane must not cost the container.

    The worker took nothing, so the request stays in flight and the next poll
    brings it back. Acknowledging it here is what stranded a `pending` row with
    nothing coming for it.
    """

    request = _request(payload={"image_id": "image-1", "startup_kind": "function"})
    workers = _WorkerRepository(requests=[request])
    containers = _ContainerRepository(
        states={"ctr-1": _state(request, status=SchedulerContainerStatus.Pending)},
        state_errors=1,
    )
    execution = _ExecutionService()
    processor = WorkerSchedulerRequestProcessor(
        build_cancels=WorkerBuildCancelRegistry(),
        worker_id="worker-1",
        workers=workers,
        containers=containers,
        execution=execution,
        worker_gpu_type="",
    )

    with pytest.raises(WorkerRepositoryClientError):
        processor.run_once()

    assert workers.acknowledged == []
    assert [entry.container_id for entry in workers.in_flight] == ["ctr-1"]

    started = processor.run_once()

    assert started.status is WorkerSchedulerRequestStatus.Executed
    assert started.background
    assert workers.acknowledged == [("worker-1", "ctr-1")]
    assert workers.in_flight == []
    _wait_for_background_result(processor, "ctr-1")
    assert [context.request.container_id for context in execution.contexts] == ["ctr-1"]


def test_worker_scheduler_request_processor_refuses_a_container_it_already_started() -> None:
    """The guard that survives a worker restart: the container's own state.

    A dispatch writes `pending` before the request is queued and only the worker
    that took it writes `running`, so a request arriving for a running container
    is one this worker already acted on and lost the acknowledgement for.
    """

    request = _request(payload={"image_id": "image-1", "startup_kind": "function"})
    workers = _WorkerRepository(requests=[request])
    containers = _ContainerRepository(
        states={"ctr-1": _state(request, status=SchedulerContainerStatus.Running)}
    )
    execution = _ExecutionService()
    processor = WorkerSchedulerRequestProcessor(
        build_cancels=WorkerBuildCancelRegistry(),
        worker_id="worker-1",
        workers=workers,
        containers=containers,
        execution=execution,
        worker_gpu_type="",
    )

    result = processor.run_once()

    assert result.status is WorkerSchedulerRequestStatus.Reconciled
    assert result.action is WorkerSchedulerRequestAction.SkipStartedContainer
    assert execution.contexts == []
    assert containers.deleted == []
    assert workers.capacity_changes == []
    assert workers.acknowledged == [("worker-1", "ctr-1")]
    assert workers.in_flight == []


def _request(
    *,
    payload: dict[str, JsonValue] | None = None,
    gpu_type: str = "",
    gpu_count: int = 0,
) -> WorkerExecutionRequest:
    return WorkerExecutionRequest(
        workspace_id="workspace-1",
        stub_id="stub-1",
        container_id="ctr-1",
        cpu_millicores=1000,
        memory_mib=512,
        gpu=[gpu_type] if gpu_type else [],
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
    request: WorkerExecutionRequest,
    *,
    status: SchedulerContainerStatus,
) -> WorkerContainerState:
    return WorkerContainerState(
        container_id=request.container_id,
        stub_id=request.stub_id,
        workspace_id=request.workspace_id,
        worker_id="worker-1",
        status=status,
    )


@dataclass(slots=True)
class _WorkerRepository:
    """Delivers at least once, exactly as the scheduler repository does.

    A request taken from the queue stays in flight and is handed back on every
    take until it is acknowledged, so a processor that ignores redelivery fails
    here rather than only against Redis.
    """

    requests: list[WorkerExecutionRequest] = field(default_factory=list)
    capacity_changes: list[tuple[str, str, WorkerCapacityChange]] = field(default_factory=list)
    acknowledged: list[tuple[str, str]] = field(default_factory=list)
    in_flight: list[WorkerExecutionRequest] = field(default_factory=list)
    acknowledge_error: str = ""

    def get_next_container_request(self, worker_id: str) -> WorkerExecutionRequest | None:
        _ = worker_id
        if self.in_flight:
            return self.in_flight[0]
        if not self.requests:
            return None
        request = self.requests.pop(0)
        self.in_flight.append(request)
        return request

    def acknowledge_worker_request(self, worker_id: str, container_id: str) -> bool:
        self.acknowledged.append((worker_id, container_id))
        if self.acknowledge_error:
            raise WorkerRepositoryClientError(self.acknowledge_error)
        remaining = [request for request in self.in_flight if request.container_id != container_id]
        acknowledged = len(remaining) < len(self.in_flight)
        self.in_flight = remaining
        return acknowledged

    def update_worker_capacity(
        self,
        worker_id: str,
        request: WorkerExecutionRequest,
        change: WorkerCapacityChange,
    ) -> WorkerCapacityResult:
        self.capacity_changes.append((worker_id, request.container_id, change))
        return WorkerCapacityResult(
            worker=WorkerExecutionRecord(
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
    states: dict[str, WorkerContainerState] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    status_updates: list[tuple[str, SchedulerContainerStatus]] = field(default_factory=list)
    exit_codes: list[tuple[str, int]] = field(default_factory=list)
    ttls: list[int] = field(default_factory=list)
    state_errors: int = 0

    def list_pending_storage_cleanup(self) -> list[str]:
        return []

    def get_container_state(self, container_id: str) -> WorkerContainerState | None:
        if self.state_errors > 0:
            self.state_errors -= 1
            raise WorkerRepositoryClientError("control plane is unreachable")
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
        exited_at: datetime,
        termination_reason: StopContainerReason = StopContainerReason.Unknown,
        failed_phase: ContainerExecutionPhase | None = None,
        failure_detail: str = "",
    ) -> None:
        del termination_reason, failed_phase, failure_detail
        self.exit_codes.append((container_id, exit_code))

    def delete_container_state(self, container_id: str, *, storage_released: bool = False) -> bool:
        self.deleted.append(container_id)
        return self.states.pop(container_id, None) is not None


@dataclass(slots=True)
class _ExecutionService:
    result: ContainerExecutionResult = field(default_factory=ContainerExecutionResult)
    contexts: list[ContainerExecutionContext] = field(default_factory=list)

    def recover_cleanup(self, container_ids: Sequence[str]) -> None:
        pass

    def execute(self, context: ContainerExecutionContext) -> ContainerExecutionResult:
        self.contexts.append(context)
        return self.result


@dataclass(slots=True)
class _BlockingExecutionService:
    started: threading.Event
    stop_requested: threading.Event
    containers: _ContainerRepository
    contexts: list[ContainerExecutionContext] = field(default_factory=list)

    def recover_cleanup(self, container_ids: Sequence[str]) -> None:
        pass

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
