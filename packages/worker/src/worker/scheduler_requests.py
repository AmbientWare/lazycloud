from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from time import monotonic
from typing import Protocol

from shared.container_requests import (
    StopContainerReason,
    WorkerContainerRequestPayload,
    WorkerStartupKind,
    container_memory_limit_mib,
)
from shared.contracts import ContractModel
from shared.gpu import concrete_gpu_type
from shared.image_building.authoring import LinuxArchitecture
from shared.scheduling import (
    DEFAULT_CONTAINER_STATE_TTL_SECONDS,
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRequest,
    WorkerCapacityChange,
    WorkerCapacityPlan,
    gpu_count_for_capacity,
)
from shared.timestamps import utc_now

from worker.container_execution import (
    ContainerExecutionContext,
    ContainerExecutionResult,
)
from worker.events import ContainerRequestContext
from worker.finalization import ContainerFinalizationRepository
from worker.image_build_execution import (
    WorkerImageBuildExecutionResult,
    is_image_build_scheduler_request,
)
from worker.image_build_requests import IMAGE_BUILD_REQUEST_KIND
from worker.memory_pressure import ResidentContainer
from worker.monitoring import WorkerUsageWindowRecorder
from worker.runtime_config import absolute_container_cgroup_path
from worker.status import (
    WorkerDeliveredRequestPlan,
    WorkerSchedulerRequestAction,
    plan_delivered_container_request,
)

LOGGER = logging.getLogger(__name__)

MIB = 1024 * 1024
WORKER_REQUEST_DELIVERY_RETRY_SECONDS = 60.0
"""How long this worker keeps retrying a delivery it could not act on.

Only a request the worker never took is retried, and only against a fault it
cannot see past—the control plane being unreachable. A time bound rather than an
attempt count, because what it has to stay under is a clock: the durable row's
start deadline does not reclaim a container while something still claims to be
delivering a request for it, so the retrying has to end well inside that window
however fast this worker polls.
"""

WORKER_REQUEST_ACKNOWLEDGEMENT_BACKOFF_SECONDS = 1.0
WORKER_REQUEST_ACKNOWLEDGEMENT_BACKOFF_MAX_SECONDS = 15.0
"""How long an acknowledgement waits before it is attempted again.

The poll loop runs many times a second and every pass retries what is still
unacknowledged, so an unreachable control plane would otherwise be answered with
one request and one warning per pass — thousands of them per worker per outage,
each one delaying the poll for work this worker could actually do. Doubling from
a second and capped, which turns the whole retry bound below into a handful of
attempts rather than a flood, and still acknowledges within a second of the
control plane coming back.
"""


class WorkerSchedulerRequestStatus(StrEnum):
    Idle = "idle"
    Dropped = "dropped"
    Reconciled = "reconciled"
    Executed = "executed"
    Error = "error"


class WorkerSchedulerRequestWorkerRepository(Protocol):
    def get_next_container_request(self, worker_id: str) -> SchedulerWorkerRequest | None: ...

    def acknowledge_worker_request(self, worker_id: str, container_id: str) -> bool: ...

    def update_worker_capacity(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
        change: WorkerCapacityChange,
    ) -> WorkerCapacityPlan: ...


class WorkerSchedulerRequestContainerRepository(ContainerFinalizationRepository, Protocol):
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...


class WorkerSchedulerRequestLifecycle(Protocol):
    def register_container(
        self,
        request: ContainerRequestContext,
    ) -> None: ...

    def unregister_container(self, container_id: str) -> None: ...


class WorkerSchedulerRequestImageBuildExecutor(Protocol):
    def execute(self, request: SchedulerWorkerRequest) -> WorkerImageBuildExecutionResult: ...


class WorkerSchedulerRequestExecutionService(Protocol):
    def execute(self, context: ContainerExecutionContext) -> ContainerExecutionResult: ...


class WorkerSchedulerRequestResult(ContractModel):
    worker_id: str
    status: WorkerSchedulerRequestStatus
    action: WorkerSchedulerRequestAction
    container_id: str = ""
    request: SchedulerWorkerRequest | None = None
    execution: ContainerExecutionResult | None = None
    image_build: WorkerImageBuildExecutionResult | None = None
    delivery: WorkerDeliveredRequestPlan | None = None
    capacity_released: bool = False
    capacity_release_error: str = ""
    state_deleted: bool = False
    background: bool = False
    error_message: str = ""

    @property
    def processed(self) -> bool:
        return self.status is not WorkerSchedulerRequestStatus.Idle


@dataclass(slots=True)
class _BackgroundExecution:
    request: SchedulerWorkerRequest
    thread: threading.Thread | None = None
    result: WorkerSchedulerRequestResult | None = None
    pid: int = 0
    """The sandbox process, once the runtime has started one."""


@dataclass(slots=True)
class _Delivery:
    """A request this worker has been handed and has not yet acknowledged.

    Held for exactly as long as the control plane could still redeliver it, which
    is what makes a redelivery recognisable: the same container arriving twice is
    the same work, not a second container to start.
    """

    first_attempt_at: float = field(default_factory=monotonic)
    attempts: int = 0
    held: bool = False
    committed: bool = False
    acknowledge_attempts: int = 0
    acknowledge_failed_at: float = 0.0
    acknowledge_after: float = 0.0


@dataclass(slots=True)
class WorkerSchedulerRequestProcessor:
    worker_id: str
    workers: WorkerSchedulerRequestWorkerRepository
    containers: WorkerSchedulerRequestContainerRepository
    execution: WorkerSchedulerRequestExecutionService
    worker_gpu_type: str
    """The GPU model this worker's machine declares it holds, empty on a CPU host.

    No default: it is what every container this worker runs is billed for, and a
    machine that silently reports the wrong card bills the wrong rate.
    """

    node_cpu_millicores: int = 0
    node_memory_mib: int = 0
    """What this worker's machine holds, or zero when it could not be read.

    Only bounds a container's hard memory ceiling, so an unreadable machine costs
    a ceiling that may be too generous rather than a container that will not run.
    """

    lifecycle: WorkerSchedulerRequestLifecycle | None = None
    image_builds: WorkerSchedulerRequestImageBuildExecutor | None = None
    usage_recorder: WorkerUsageWindowRecorder | None = None
    """Meters an image build, which runs here rather than under the runtime
    monitor an ordinary container is watched by. Required to run a build at all:
    a worker that cannot report what a build consumed would give the capacity
    away."""

    _background: dict[str, _BackgroundExecution] = field(default_factory=dict, init=False)
    _deliveries: dict[str, _Delivery] = field(default_factory=dict, init=False)
    _delivery_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def run_once(self) -> WorkerSchedulerRequestResult:
        self._retry_acknowledgements()
        completed = self._pop_completed_background()
        if completed is not None:
            return completed

        request = self.workers.get_next_container_request(self.worker_id)
        if request is None:
            return WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Idle,
                action=WorkerSchedulerRequestAction.Idle,
            )

        if not self._begin_delivery(request):
            # The same request arriving again while this worker still holds it is
            # the control plane not having heard the acknowledgement, not a second
            # container to start. Nothing here runs, releases capacity, or touches
            # the container's state; the retry above is what ends the redelivery.
            return WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Reconciled,
                action=WorkerSchedulerRequestAction.ReconcileDelivery,
                container_id=request.container_id,
                request=request,
            )

        try:
            result = self._process_request(request)
        except Exception:
            # Reaching here means the control plane could not be asked what to do
            # with the request, so this worker has taken nothing. Acknowledging it
            # would throw the container away for a fault that has already passed
            # by the next poll, which is the whole failure this path removes.
            self._end_delivery(request.container_id, resolved=False)
            raise
        self._end_delivery(request.container_id, resolved=True)
        return result

    def _process_request(self, request: SchedulerWorkerRequest) -> WorkerSchedulerRequestResult:
        state = self.containers.get_container_state(request.container_id)
        delivery = plan_delivered_container_request(
            state_missing=state is None,
            state_status=state.status if state is not None else None,
        )
        if delivery.drop:
            return self._drop_request(request, delivery)

        if is_image_build_scheduler_request(request):
            return self._release_capacity(request, self._execute_image_build_request(request))

        try:
            context = container_execution_context_from_scheduler_request(
                request,
                worker_gpu_type=self.worker_gpu_type,
                node_cpu_millicores=self.node_cpu_millicores,
                node_memory_mib=self.node_memory_mib,
            )
            if runs_in_background(context.startup_kind):
                return self._start_background(request, context)
            execution = self._execute_container(context)
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            result = WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=request.container_id,
                request=request,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        else:
            result = WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=(
                    WorkerSchedulerRequestStatus.Executed
                    if execution.ok
                    else WorkerSchedulerRequestStatus.Error
                ),
                action=WorkerSchedulerRequestAction.Execute,
                container_id=request.container_id,
                request=request,
                execution=execution,
                error_message=(
                    ""
                    if execution.ok
                    else f"worker execution failed at {execution.failed_phase.value}"
                    if execution.failed_phase is not None
                    else "worker execution failed"
                ),
            )
        return self._release_capacity(request, result)

    def _begin_delivery(self, request: SchedulerWorkerRequest) -> bool:
        """Take this delivery, or refuse it because the worker already holds it."""

        with self._delivery_lock:
            delivery = self._deliveries.get(request.container_id)
            if delivery is not None and (delivery.held or delivery.committed):
                return False
            if delivery is None:
                delivery = _Delivery()
                self._deliveries[request.container_id] = delivery
            delivery.attempts += 1
            delivery.held = True
            return True

    def _commit_delivery(self, container_id: str) -> None:
        """Tell the control plane this worker holds the container, and stop holding the request.

        Called where the worker has taken local ownership—the container is in its
        active set, or the build it names has been marked running—so a worker that
        dies before that point has its request redelivered rather than dropped.
        What remains after it is the durable row's start deadline, which covers a
        worker that acknowledged and then died before the container came up.
        """

        with self._delivery_lock:
            delivery = self._deliveries.get(container_id)
            if delivery is None:
                return
            delivery.committed = True
        self._acknowledge(container_id)

    def _end_delivery(self, container_id: str, *, resolved: bool) -> None:
        """Close a delivery out: acknowledge it, or hand it back to be redelivered.

        A request the worker resolved is acknowledged however it ended—started,
        dropped, or recognised as one it had already run. A request it never got
        to look at is left in flight so the next poll brings it back, and only a
        worker that cannot make progress on it at all gives up: the durable row's
        start deadline cannot reclaim a container while a request for it is still
        live, so retrying here has to be bounded rather than endless.
        """

        with self._delivery_lock:
            delivery = self._deliveries.get(container_id)
            if delivery is None:
                return
            delivery.held = False
            attempts = delivery.attempts
            retrying = (
                monotonic() - delivery.first_attempt_at
            ) < WORKER_REQUEST_DELIVERY_RETRY_SECONDS
            if not delivery.committed:
                if not resolved and retrying:
                    LOGGER.warning(
                        "container request could not be acted on; leaving it for redelivery",
                        extra={
                            "worker_id": self.worker_id,
                            "container_id": container_id,
                            "attempts": attempts,
                        },
                    )
                    return
                delivery.committed = True
                if not resolved:
                    LOGGER.warning(
                        "container request giving up after repeated failures",
                        extra={
                            "worker_id": self.worker_id,
                            "container_id": container_id,
                            "attempts": attempts,
                        },
                    )
        self._acknowledge(container_id)

    def _acknowledge(self, container_id: str) -> None:
        try:
            self.workers.acknowledge_worker_request(self.worker_id, container_id)
        except Exception as exc:
            self._defer_acknowledgement(container_id, exc)
            return
        with self._delivery_lock:
            self._deliveries.pop(container_id, None)

    def _defer_acknowledgement(self, container_id: str, error: Exception) -> None:
        """Schedule the next attempt at an acknowledgement, or stop making them.

        The control plane is what is unreachable, so the request is still in
        flight and will be handed back. Holding the delivery here is what makes
        that redelivery a no-op instead of a second container — but only for as
        long as the same outage can be believed, which is the bound the
        uncommitted path is held to and for the same reason.

        Past it the delivery is let go rather than retried forever: a redelivery
        that arrives after this worker has forgotten it is still reconciled
        against the container's own state, which already says the container is
        running or has run, so what it costs is a dropped request rather than a
        second container. Keeping the entry instead would grow this map for every
        container the worker ever ran.
        """

        now = monotonic()
        with self._delivery_lock:
            delivery = self._deliveries.get(container_id)
            if delivery is None:
                return
            if delivery.acknowledge_attempts == 0:
                delivery.acknowledge_failed_at = now
            delivery.acknowledge_attempts += 1
            attempts = delivery.acknowledge_attempts
            abandoned = (
                now - delivery.acknowledge_failed_at
            ) >= WORKER_REQUEST_DELIVERY_RETRY_SECONDS
            if abandoned:
                del self._deliveries[container_id]
            else:
                delivery.acknowledge_after = now + _acknowledgement_backoff(attempts)
        LOGGER.warning(
            "container request acknowledgement failed; giving up"
            if abandoned
            else "container request acknowledgement failed; will retry",
            extra={
                "worker_id": self.worker_id,
                "container_id": container_id,
                "attempts": attempts,
                "error": f"{type(error).__name__}: {error}",
            },
        )

    def _retry_acknowledgements(self) -> None:
        now = monotonic()
        with self._delivery_lock:
            pending = [
                container_id
                for container_id, delivery in self._deliveries.items()
                if delivery.committed and not delivery.held and delivery.acknowledge_after <= now
            ]
        for container_id in pending:
            self._acknowledge(container_id)

    def _take_container(self, context: ContainerExecutionContext) -> None:
        if self.lifecycle is not None:
            self.lifecycle.register_container(context.request)
        self._commit_delivery(context.request.container_id)

    def _release_container(self, container_id: str) -> None:
        if self.lifecycle is not None:
            self.lifecycle.unregister_container(container_id)

    def _execute_container(self, context: ContainerExecutionContext) -> ContainerExecutionResult:
        self._take_container(context)
        try:
            return self.execution.execute(context)
        finally:
            self._release_container(context.request.container_id)

    def _start_background(
        self,
        request: SchedulerWorkerRequest,
        context: ContainerExecutionContext,
    ) -> WorkerSchedulerRequestResult:
        # Taken here rather than on the thread: this call returns to a loop that
        # polls again immediately, and the acknowledgement has to be in flight
        # before it does or every background start costs a redelivery.
        self._take_container(context)
        active = _BackgroundExecution(request=request)
        thread = threading.Thread(
            target=self._run_background,
            args=(active, context),
            name=f"container-exec-{request.container_id}",
            daemon=True,
        )
        active.thread = thread
        self._background[request.container_id] = active
        thread.start()
        return WorkerSchedulerRequestResult(
            worker_id=self.worker_id,
            status=WorkerSchedulerRequestStatus.Executed,
            action=WorkerSchedulerRequestAction.Execute,
            container_id=request.container_id,
            request=request,
            background=True,
        )

    def record_container_started(self, container_id: str, pid: int) -> None:
        """Note the sandbox a running container got.

        Called from the execution thread the moment the runtime reports one. A
        long-running container has no pid when it is registered and reports none
        again until it exits, so without this it is invisible to anything reading
        live containers.
        """
        active = self._background.get(container_id)
        if active is not None:
            active.pid = pid

    def resident_containers(self) -> list[ResidentContainer]:
        """The containers this worker is holding, and the cgroup accounting for each.

        Background executions only. A foreground container runs inside the call
        that started it, so the loop asking this question is not running while
        one exists.

        The cgroup rather than the sandbox's pid: a pid is reused by Linux once
        the process it named exits, so a container recorded as it went away could
        hand the watcher a live pid belonging to something else entirely, whose
        size would then be weighed against this container's reservation. A cgroup
        path names the container and nothing else, and reads as gone rather than
        as someone else.
        """
        return [
            ResidentContainer(
                container_id=container_id,
                cgroup_path=absolute_container_cgroup_path(container_id),
            )
            for container_id in self._background
        ]

    def _run_background(
        self,
        active: _BackgroundExecution,
        context: ContainerExecutionContext,
    ) -> None:
        try:
            execution = self._execute_registered_container(context)
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            result = WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=active.request.container_id,
                request=active.request,
                background=True,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        else:
            result = WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=(
                    WorkerSchedulerRequestStatus.Executed
                    if execution.ok
                    else WorkerSchedulerRequestStatus.Error
                ),
                action=WorkerSchedulerRequestAction.Execute,
                container_id=active.request.container_id,
                request=active.request,
                execution=execution,
                background=True,
                error_message=(
                    ""
                    if execution.ok
                    else f"worker execution failed at {execution.failed_phase.value}"
                    if execution.failed_phase is not None
                    else "worker execution failed"
                ),
            )
        active.result = self._release_capacity(active.request, result)

    def _execute_registered_container(
        self,
        context: ContainerExecutionContext,
    ) -> ContainerExecutionResult:
        try:
            return self.execution.execute(context)
        finally:
            self._release_container(context.request.container_id)

    def _pop_completed_background(self) -> WorkerSchedulerRequestResult | None:
        for container_id, active in tuple(self._background.items()):
            thread = active.thread
            if thread is not None and thread.is_alive():
                continue
            if thread is not None:
                thread.join(timeout=0)
            self._background.pop(container_id, None)
            if active.result is not None:
                return active.result
            return WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=container_id,
                request=active.request,
                background=True,
                error_message="background container execution ended without a result",
            )
        return None

    def _execute_image_build_request(
        self,
        request: SchedulerWorkerRequest,
    ) -> WorkerSchedulerRequestResult:
        image_builds = self.image_builds
        usage_recorder = self.usage_recorder
        if image_builds is None or usage_recorder is None:
            return WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=request.container_id,
                request=request,
                error_message=(
                    "image build executor is not configured"
                    if image_builds is None
                    else "image build usage recorder is not configured"
                ),
            )
        try:
            self.containers.update_container_status(
                request.container_id,
                SchedulerContainerStatus.Running,
                ttl_seconds=DEFAULT_CONTAINER_STATE_TTL_SECONDS,
            )
            # A build has no container to register, so the status it just wrote is
            # the record that this worker holds it.
            self._commit_delivery(request.container_id)
            image_build = self._metered_image_build(
                request,
                image_builds,
                usage_recorder,
            )
            self.containers.set_exit_code(
                request.container_id,
                0 if image_build.ok else 1,
                termination_reason=StopContainerReason.Unknown,
            )
            self.containers.update_container_status(
                request.container_id,
                SchedulerContainerStatus.Complete
                if image_build.ok
                else SchedulerContainerStatus.Failed,
                ttl_seconds=DEFAULT_CONTAINER_STATE_TTL_SECONDS,
            )
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            try:
                self.containers.set_exit_code(
                    request.container_id,
                    1,
                    termination_reason=StopContainerReason.Unknown,
                )
                self.containers.update_container_status(
                    request.container_id,
                    SchedulerContainerStatus.Failed,
                    ttl_seconds=DEFAULT_CONTAINER_STATE_TTL_SECONDS,
                )
            except Exception:
                LOGGER.warning(
                    "could not record %s as failed",
                    request.container_id,
                    exc_info=True,
                )
            return WorkerSchedulerRequestResult(
                worker_id=self.worker_id,
                status=WorkerSchedulerRequestStatus.Error,
                action=WorkerSchedulerRequestAction.Execute,
                container_id=request.container_id,
                request=request,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        return WorkerSchedulerRequestResult(
            worker_id=self.worker_id,
            status=(
                WorkerSchedulerRequestStatus.Executed
                if image_build.ok
                else WorkerSchedulerRequestStatus.Error
            ),
            action=WorkerSchedulerRequestAction.Execute,
            container_id=request.container_id,
            request=request,
            image_build=image_build,
            error_message=image_build.error_message,
        )

    def _metered_image_build(
        self,
        request: SchedulerWorkerRequest,
        image_builds: WorkerSchedulerRequestImageBuildExecutor,
        usage_recorder: WorkerUsageWindowRecorder,
    ) -> WorkerImageBuildExecutionResult:
        """Run the build and bill the capacity it held while it ran.

        A build that fails held the same cpu, memory and card as one that
        succeeds, for as long as it ran, so the window is reported whichever way
        it ends and whatever it produced. Reported before the container is
        marked finished, so the window closes inside the lifetime the control
        plane holds rather than an instant past it.

        The window is measured monotonically and the wall-clock end derived from
        it, so the quantity billed and the interval it is priced over cannot
        disagree when the host's clock steps.
        """

        started_at = monotonic()
        started_at_utc = utc_now()
        try:
            return image_builds.execute(request)
        finally:
            duration_ms = max(int((monotonic() - started_at) * 1000), 1)
            try:
                usage_recorder.record_usage_window(
                    image_build_request_context(
                        request,
                        worker_gpu_type=self.worker_gpu_type,
                    ),
                    duration_ms=duration_ms,
                    window_start_ms=0,
                    window_end_ms=duration_ms,
                    metering_window_started_at=started_at_utc,
                    metering_window_ended_at=started_at_utc + timedelta(milliseconds=duration_ms),
                )
            except Exception:
                # The control plane holds the durable trace through the worker
                # event the recorder publishes; losing it must not also lose the
                # build's own result.
                LOGGER.warning(
                    "image build usage window was not recorded",
                    exc_info=True,
                    extra={
                        "container_id": request.container_id,
                        "duration_ms": duration_ms,
                    },
                )

    def _drop_request(
        self,
        request: SchedulerWorkerRequest,
        delivery: WorkerDeliveredRequestPlan,
    ) -> WorkerSchedulerRequestResult:
        state_deleted = False
        if delivery.delete_state:
            state_deleted = self.containers.delete_container_state(request.container_id)
        LOGGER.warning(
            "container request will not run: %s",
            delivery.reason,
            extra={"worker_id": self.worker_id, "container_id": request.container_id},
        )
        result = WorkerSchedulerRequestResult(
            worker_id=self.worker_id,
            status=(
                WorkerSchedulerRequestStatus.Reconciled
                if delivery.action
                in {
                    WorkerSchedulerRequestAction.DropFinishedState,
                    WorkerSchedulerRequestAction.SkipStartedContainer,
                }
                else WorkerSchedulerRequestStatus.Dropped
            ),
            action=delivery.action,
            container_id=request.container_id,
            request=request,
            delivery=delivery,
            state_deleted=state_deleted,
        )
        if not delivery.release_capacity:
            # The container this names is running or has run, so its reservation is
            # released by the execution that holds it. Releasing again here would
            # hand the worker capacity it never got back.
            return result
        return self._release_capacity(request, result)

    def _release_capacity(
        self,
        request: SchedulerWorkerRequest,
        result: WorkerSchedulerRequestResult,
    ) -> WorkerSchedulerRequestResult:
        try:
            self.workers.update_worker_capacity(
                self.worker_id,
                request,
                WorkerCapacityChange.Add,
            )
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            return result.model_copy(
                update={
                    "status": WorkerSchedulerRequestStatus.Error,
                    "capacity_release_error": f"{type(exc).__name__}: {exc}",
                }
            )
        return result.model_copy(update={"capacity_released": True})


def _acknowledgement_backoff(attempts: int) -> float:
    steps = min(max(attempts - 1, 0), 16)
    doubled = WORKER_REQUEST_ACKNOWLEDGEMENT_BACKOFF_SECONDS * 2**steps
    return min(doubled, WORKER_REQUEST_ACKNOWLEDGEMENT_BACKOFF_MAX_SECONDS)


def _billable_gpu(*, gpu_count: int, worker_gpu_type: str) -> str:
    """Which GPU model this container is charged for.

    Empty whenever no GPU was allocated — a CPU-only container on a GPU host must
    not be stamped with that host's card. Where one was allocated only the
    machine's own model counts: the request carries what the user asked for, and
    charging that would bill a card nothing on the machine confirmed. A worker
    that cannot name its card therefore bills as an unpriced gap rather than a
    guess.
    """

    if gpu_count <= 0:
        return ""
    return concrete_gpu_type(worker_gpu_type)


def image_build_request_context(
    request: SchedulerWorkerRequest,
    *,
    worker_gpu_type: str,
) -> ContainerRequestContext:
    """What an image build is billed for.

    The capacity the control plane placed the build with, read from the request
    it dispatched — the same figures it recorded the placement from, so the
    metered window and the priced shape describe one container.
    """

    gpu_count = gpu_count_for_capacity(request.gpu_type, request.gpu_request, request.gpu_count)
    return ContainerRequestContext(
        container_id=request.container_id,
        stub_id=request.stub_id,
        stub_type=IMAGE_BUILD_REQUEST_KIND,
        workspace_id=request.workspace_id,
        cpu_millicores=request.cpu_millicores,
        memory_mib=request.memory_mib,
        gpu=_billable_gpu(gpu_count=gpu_count, worker_gpu_type=worker_gpu_type),
        gpu_count=gpu_count,
    )


def container_execution_context_from_scheduler_request(
    request: SchedulerWorkerRequest,
    *,
    worker_gpu_type: str,
    node_cpu_millicores: int = 0,
    node_memory_mib: int = 0,
) -> ContainerExecutionContext:
    """Build the execution context for one scheduled container.

    `worker_gpu_type` is what this worker's machine declares it holds, and is what
    the container is billed for. The request carries whatever the user asked for,
    which may be `any` or a list, and neither of those is a rate anything can
    price.
    """
    payload = WorkerContainerRequestPayload.model_validate(request.payload)
    # Resolved once, here, because two things downstream read it and they must
    # read the same number: the cgroup the container runs under, and the watcher
    # that reports an OOM. Watching the request while the cgroup allowed a
    # multiple of it would report a container killed for using what it was given.
    memory_limit_bytes = payload.memory_limit_bytes
    if memory_limit_bytes is None and request.memory_mib > 0:
        memory_limit_bytes = container_memory_limit_mib(request.memory_mib) * MIB
    # One allocation count, so the model and the count it is charged by can never
    # disagree about whether this container held a GPU at all.
    gpu_count = gpu_count_for_capacity(request.gpu_type, request.gpu_request, request.gpu_count)
    return ContainerExecutionContext(
        request=ContainerRequestContext(
            container_id=request.container_id,
            image_id=payload.image_id,
            archive_sha256=payload.archive_sha256,
            stub_id=request.stub_id,
            stub_type=payload.stub_type,
            workspace_id=request.workspace_id,
            workspace_name=payload.workspace_name,
            app_id=payload.app_id,
            deployment_id=payload.deployment_id,
            env=list(payload.env),
            secret_names=list(payload.secret_names),
            gateway_token_required=payload.gateway_token_required,
            workspace_storage_required=payload.workspace_storage_required,
            mounts=list(payload.mounts),
            workspace_storage_available=payload.workspace_storage_available,
            workspace_storage_base_mount_path=payload.workspace_storage_base_mount_path,
            cpu_millicores=request.cpu_millicores,
            memory_mib=request.memory_mib,
            disk_limit_bytes=payload.disk_limit_bytes or 0,
            gpu=_billable_gpu(gpu_count=gpu_count, worker_gpu_type=worker_gpu_type),
            gpu_count=gpu_count,
        ),
        architecture=LinuxArchitecture(request.architecture),
        startup_kind=payload.startup_kind,
        ports=list(payload.ports),
        requested_ports=list(payload.requested_ports),
        checkpoint_exposed_ports=list(payload.checkpoint_exposed_ports),
        checkpoint_id=payload.checkpoint_id,
        checkpoint_enabled=payload.checkpoint_enabled,
        checkpoint_readiness_path=payload.checkpoint_readiness_path,
        checkpoint_readiness_port=payload.checkpoint_readiness_port,
        checkpoint_readiness_timeout_seconds=payload.checkpoint_readiness_timeout_seconds,
        checkpoint_readiness_interval_seconds=payload.checkpoint_readiness_interval_seconds,
        entrypoint=list(payload.entrypoint),
        cwd=payload.cwd,
        runtime=payload.runtime,
        docker_enabled=payload.docker_enabled,
        block_network=payload.block_network,
        allow_list=list(payload.allow_list),
        memory_enforced=payload.memory_enforced,
        memory_limit_bytes=memory_limit_bytes,
        cpu_limit_millicores=payload.cpu_limit_millicores,
        node_cpu_millicores=node_cpu_millicores,
        node_memory_mib=node_memory_mib,
        cgroup_path=payload.cgroup_path,
        run_delayed_cleanup=payload.run_delayed_cleanup,
    )


def runs_in_background(kind: WorkerStartupKind) -> bool:
    return kind in {
        WorkerStartupKind.Function,
        WorkerStartupKind.Endpoint,
        WorkerStartupKind.Asgi,
        WorkerStartupKind.Pod,
        WorkerStartupKind.PodRun,
        WorkerStartupKind.Sandbox,
    }
