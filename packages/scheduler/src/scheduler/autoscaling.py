from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import Protocol

from coordination.redis_client import RedisClient
from database.records.apps import StubKind, StubRecord
from pydantic import Field, JsonValue
from shared.autoscaler_state import (
    AutoscalerStateRecord,
    AutoscalerTargetKind,
    autoscaler_state_name,
)
from shared.autoscaling import (
    BacklogAutoscalerConfig,
    BacklogAutoscalerSample,
    PodAutoscalerConfig,
    PodAutoscalerSample,
    PodContainerState,
    PodStubType,
    ScaleDecisionKind,
    decide_backlog_scale,
    decide_pod_scale,
    function_container_ceiling,
    scale_kind,
    select_stoppable_pod_containers,
)
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.errors import DomainError, InvalidInputError, NotFoundError
from shared.http.endpoints import StartEndpointServeRequest, StartEndpointServeResponse
from shared.http.pods import CreatePodRequest, CreatePodResponse
from shared.scheduling import SchedulerContainerStatus
from shared.timestamps import utc_now
from shared.worker_events import (
    ENDPOINT_SCALE_DECISION_ACTION,
    FUNCTION_SCALE_DECISION_ACTION,
    POD_SCALE_DECISION_ACTION,
)
from shared.workload_config import StubConfig
from shared.workload_keys import (
    pod_container_connections_key,
    pod_keep_warm_lock_key,
    pod_total_connections_key,
)

from scheduler.autoscaling_guardrails import (
    AutoscalerGuardrailPlan,
    plan_autoscaler_start_guardrails,
)
from scheduler.services import (
    SchedulerServices,
)

AUTOSCALER_LOCK_TTL_SECONDS = 10
AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD = 3
AUTOSCALER_DEFAULT_FAILURE_WINDOW_SECONDS = 300
CONTAINER_START_DEADLINE_SECONDS = 600
"""How long an unheld `pending` container may sit before it stops being capacity.

Bounded from both ends. Below it, a start still in progress must not be reaped:
the reclaim frees the ceiling, the next tick starts a replacement, and if the
deadline is shorter than the real start that replacement is reaped at the same
age — a workload that never runs at all, where the symptom was one that ran
late. Above it, nothing is gained: a stranded container is already recovered at
about sixteen minutes, when its scheduler state lapses and its worker's orphan
path fails it, so a deadline past that would only re-describe what already
happens and would still leave the recovery owned by a cache expiry.

Six hundred is the platform's own figure for how long a pending container may
legitimately show no progress — the window a worker re-arms that container's
scheduler state for while it is still pulling — and it lands a clear six minutes
inside the sixteen. It is a bound on a start already in somebody's hands, not on
the wait for capacity: a request still queued for placement is read directly
below, so the clock never has to allow for the fifteen minutes the dispatcher
may spend retrying one.
"""
FUNCTION_AUTOSCALER_SOURCE = "function.autoscaler"
ENDPOINT_AUTOSCALER_DEFAULT_TIMEOUT_SECONDS = 600
ENDPOINT_AUTOSCALER_SOURCE = "endpoint.autoscaler"
POD_AUTOSCALER_SOURCE = "pod.autoscaler"


class PodControl(Protocol):
    def create_pod(self, request: CreatePodRequest) -> CreatePodResponse: ...

    def expire_pods(self, *, now: datetime | None = None) -> list[ContainerRecord]: ...


class SchedulerContainerStateReader(Protocol):
    def container_statuses(
        self,
        container_ids: Sequence[str],
    ) -> dict[str, SchedulerContainerStatus]: ...


class ContainerRequestReader(Protocol):
    def has_recoverable_container_request(
        self,
        container_id: str,
        *,
        worker_id: str = "",
    ) -> bool: ...


class FunctionAutoscaleControl(Protocol):
    def start_function_container(self, stub_id: str) -> bool: ...

    def unclaimed_task_count(self, stub_id: str) -> int: ...

    def containers_holding_work(self, container_ids: Sequence[str]) -> set[str]: ...


class EndpointAutoscaleControl(Protocol):
    def start_endpoint_serve(
        self,
        request: StartEndpointServeRequest,
    ) -> StartEndpointServeResponse: ...


@dataclass(frozen=True, slots=True)
class EndpointAutoscalingDispatchObservation:
    container_id: str | None
    active: bool
    finished_at: datetime | None


class EndpointAutoscalingDispatchReader(Protocol):
    def active_count(self, stub_id: str) -> int: ...

    def list_by_stub(self, stub_id: str) -> list[EndpointAutoscalingDispatchObservation]: ...


class AutoscaleAction(ContractModel):
    container_id: str = ""
    action: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StaleContainer:
    """A record holding a ceiling slot, and the finding that says it should not."""

    record: ContainerRecord
    reason: str


class AutoscaleResult(ContractModel):
    """What one tick concluded about one workload.

    `kind` is what separates the three, and the sample is carried as a name and
    a number rather than a field per kind, so every reader of the autoscaler
    surface — the state row, the metrics, the history, the reconcile output —
    has one shape and no per-kind branch.
    """

    kind: AutoscalerTargetKind
    stub_id: str
    workspace_id: str
    signal_name: str = ""
    signal_value: int = 0
    current_containers: int = 0
    pending_containers: int = 0
    desired_containers: int = 0
    decision: ScaleDecisionKind
    reason: str
    active: bool = True
    lock_acquired: bool = True
    valid: bool = True
    failed_containers: list[str] = Field(default_factory=list)
    actions: list[AutoscaleAction] = Field(default_factory=list)
    guardrails: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.actions)


@dataclass(frozen=True, slots=True)
class AutoscalerIdentity:
    """Everything that differs between kinds and is not a decision."""

    kind: AutoscalerTargetKind
    source: str
    scale_decision_action: str
    signal_name: str
    lock_namespace: str


FUNCTION_AUTOSCALER = AutoscalerIdentity(
    kind=AutoscalerTargetKind.Function,
    source=FUNCTION_AUTOSCALER_SOURCE,
    scale_decision_action=FUNCTION_SCALE_DECISION_ACTION,
    signal_name="unclaimed_tasks",
    lock_namespace="functions",
)
ENDPOINT_AUTOSCALER = AutoscalerIdentity(
    kind=AutoscalerTargetKind.Endpoint,
    source=ENDPOINT_AUTOSCALER_SOURCE,
    scale_decision_action=ENDPOINT_SCALE_DECISION_ACTION,
    signal_name="active_requests",
    lock_namespace="endpoints",
)
POD_AUTOSCALER = AutoscalerIdentity(
    kind=AutoscalerTargetKind.Pod,
    source=POD_AUTOSCALER_SOURCE,
    scale_decision_action=POD_SCALE_DECISION_ACTION,
    signal_name="total_connections",
    lock_namespace="pods",
)


@dataclass(frozen=True, slots=True)
class ScalePlan:
    """What a workload wants, before the driver decides what it may have.

    Everything a workload knows and the driver does not: the count its own
    signal argues for, and the configured limits the record arm reports. The
    safety that turns this into what actually happens is not here.
    """

    desired_containers: int
    reason: str
    decision: ScaleDecisionKind
    valid: bool = True
    max_containers: int = 0
    min_containers: int = 0
    tasks_per_container: int = 1


class WorkloadAutoscaler(Protocol):
    """The part of autoscaling that is genuinely per kind.

    A function scales on backlog depth, an endpoint on in-flight dispatches, a
    pod on connections; they start containers three different ways and disagree
    about which of them may be stopped. That is the whole of it. Deliberately no
    reach into the pass that runs around this: what a workload cannot see, it
    cannot forget to do.

    Which containers count as capacity is not among the differences. A record the
    scheduler no longer backs is not a container of any kind, and answering that
    per workload is how two of the three came to answer it with "all of them".
    """

    @property
    def identity(self) -> AutoscalerIdentity: ...

    def selects(self, stub: StubRecord) -> bool: ...

    def sample(self, stub: StubRecord) -> int: ...

    def plan(self, stub: StubRecord, *, signal: int, current: int) -> ScalePlan: ...

    def start_one(self, stub: StubRecord) -> str | None:
        """Start one container and name it, or answer `None` to stop asking.

        `None` is a refusal the platform already made — a ceiling reached, no
        work to warrant one — and not a failure worth recording. Raise
        `DomainError` for that, and the driver records it and stops.
        """
        ...

    def scale_down(
        self,
        stub: StubRecord,
        containers: list[ContainerRecord],
        count: int,
        *,
        scheduler_statuses: Mapping[str, SchedulerContainerStatus],
        active_instance: bool,
        now: datetime,
    ) -> list[AutoscaleAction]: ...


@dataclass(slots=True)
class AutoscalingDriver:
    """The reconcile pass every workload gets, identically.

    Stub selection and the pause an operator set, the stub's lock and the state
    a contended tick still records, the container counts and which of them are
    real, the failed-container threshold, the inactive deployment, the workspace
    guardrail, and the metrics and event and state row a tick leaves behind: all
    of it here, once, for every kind, and none of it reachable from a workload.
    """

    services: SchedulerServices
    redis: RedisClient
    workload: WorkloadAutoscaler
    container_states: SchedulerContainerStateReader
    container_requests: ContainerRequestReader

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[AutoscaleResult]:
        current_time = now or utc_now()
        stubs = [
            stub
            for stub in self.services.scheduler_workloads.list_stubs()
            if self.workload.selects(stub) and _autoscaling_enabled(stub)
        ]
        results: list[AutoscaleResult] = []
        for stub in stubs[: max(limit, 0)]:
            token = token_urlsafe(16)
            lock_key = self._lock_key(stub)
            if not self._acquire_lock(lock_key, token):
                results.append(self._record_lock_contention(stub, lock_key))
                continue
            try:
                results.append(self.reconcile_stub(stub, now=current_time))
            finally:
                self._release_lock(lock_key, token)
        return results

    def reconcile_stub(
        self,
        stub: StubRecord,
        *,
        now: datetime | None = None,
    ) -> AutoscaleResult:
        current_time = now or utc_now()
        identity = self.workload.identity
        active = _deployment_active(self.services, stub)
        containers = _containers_for_stub(self.services, stub)
        scheduler_statuses = self.container_states.container_statuses(
            [container.id for container in _active_containers(containers)]
        )
        holding, stale = _partition_backed_containers(
            containers,
            scheduler_statuses,
            self.container_requests,
            now=current_time,
        )
        actions = self._recover(stale)
        current = len(holding)
        pending = _pending_container_count(holding)
        signal = self.workload.sample(stub)
        plan = self.workload.plan(stub, signal=signal, current=current)
        # A container that fails on startup frees the slot it was counted in, so
        # the signal still reads as unserved and the next tick provisions again.
        # `max_containers` does not bound that — nothing is ever alive to count
        # against it — so a stub whose startup cannot succeed is started for as
        # long as the pressure sits there. This is what bounds it, and the
        # workspace guardrail below is what bounds the healthy case.
        failed_containers = _recent_failed_container_ids(
            containers,
            now=current_time,
            window_seconds=_failed_container_window_seconds(stub.config),
        )
        failure_threshold = _failed_container_threshold(stub.config)
        failure_threshold_reached = (
            failure_threshold > 0 and len(failed_containers) >= failure_threshold
        )
        desired = plan.desired_containers
        reason = plan.reason
        decision = plan.decision
        if not active:
            desired = 0
            reason = "deployment inactive"
            decision = scale_kind(desired, current)
        elif failure_threshold_reached:
            desired = 0
            reason = "failed container threshold reached"
            decision = scale_kind(desired, current)
        guardrail = AutoscalerGuardrailPlan()
        if active and plan.valid and desired > current:
            guardrail = plan_autoscaler_start_guardrails(
                self.redis,
                stub=stub,
                current_count=current,
                desired_count=desired,
            )
            if guardrail.limited:
                desired = guardrail.desired_count
                reason = guardrail.reason
                decision = scale_kind(desired, current)
        delta = desired - current
        if delta > 0 and active and plan.valid:
            actions.extend(self._start(stub, delta))
        elif delta < 0:
            actions.extend(
                self.workload.scale_down(
                    stub,
                    holding,
                    -delta,
                    scheduler_statuses=scheduler_statuses,
                    active_instance=active and not failure_threshold_reached,
                    now=current_time,
                )
            )
        result = AutoscaleResult(
            kind=identity.kind,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            signal_name=identity.signal_name,
            signal_value=signal,
            current_containers=current,
            pending_containers=pending,
            desired_containers=desired,
            decision=decision,
            reason=reason,
            active=active,
            valid=plan.valid,
            failed_containers=failed_containers,
            actions=actions,
            guardrails=guardrail.payload(),
        )
        self._record(stub, result, plan)
        return result

    def _recover(self, stale: list[StaleContainer]) -> list[AutoscaleAction]:
        """Give back the ceiling slots that records nothing backs are holding.

        Stopped rather than deleted, and stopped through the one settlement path
        every other platform-owned stop takes, so an invocation this container
        had claimed is released back to the queue instead of being reported to
        its caller as cancelled.

        A second scheduler that reached the same conclusion — the stub lock is
        held for a bounded time, so two ticks can overlap on a slow pass — finds
        the record already terminal and stops there: the stop reads the row
        first and settles nothing twice.
        """

        actions: list[AutoscaleAction] = []
        for container in stale:
            stopped = self.services.containers.stop(
                container.record.id,
                reason=StopContainerReason.Scheduler,
            )
            actions.append(
                AutoscaleAction(
                    container_id=stopped.id,
                    action="recover-stale",
                    reason=container.reason,
                )
            )
        return actions

    def _start(self, stub: StubRecord, count: int) -> list[AutoscaleAction]:
        """Ask for containers one at a time until the workload stops giving them.

        Here rather than in each workload so that one event carries one name: the
        action is a metric label and a state-row entry, and three loops spelled it
        three ways with two different renderings of the same refusal.
        """

        actions: list[AutoscaleAction] = []
        for _ in range(count):
            try:
                container_id = self.workload.start_one(stub)
            except DomainError as exc:
                actions.append(AutoscaleAction(action="scale-up-failed", reason=exc.message))
                break
            if container_id is None:
                break
            actions.append(
                AutoscaleAction(
                    container_id=container_id,
                    action="start",
                    reason="pressure requires more containers",
                )
            )
        return actions

    def _record(self, stub: StubRecord, result: AutoscaleResult, plan: ScalePlan) -> None:
        identity = self.workload.identity
        action_payloads = _autoscale_action_payloads(result.actions)
        if _should_persist_scale_event(
            self.services,
            target_kind=identity.kind,
            stub=stub,
            decision=result.decision.value,
            desired_count=result.desired_containers,
            reason=result.reason,
            valid=result.valid,
            actions_taken=bool(result.actions),
        ):
            event_data: dict[str, JsonValue] = {
                "source": identity.source,
                "stub_id": stub.id,
                "workspace_id": stub.workspace_id,
                "kind": stub.kind.value,
                "current_containers": result.current_containers,
                "pending_containers": result.pending_containers,
                "desired_containers": result.desired_containers,
                "signal_name": result.signal_name,
                "signal_value": result.signal_value,
                "failed_containers": list(result.failed_containers),
                "decision": result.decision.value,
                "reason": result.reason,
                "valid": result.valid,
                "max_containers": plan.max_containers,
                "min_containers": plan.min_containers,
                "tasks_per_container": plan.tasks_per_container,
                "guardrails": result.guardrails,
                "actions": [*action_payloads],
            }
            self.services.events.emit(
                identity.scale_decision_action,
                resource_type="stub",
                resource_id=stub.id,
                message=f"{identity.kind.value} autoscaler selected desired container count",
                data=event_data,
                workspace_id=stub.workspace_id,
            )
        _record_autoscaler_metrics(
            self.services,
            source=identity.source,
            stub=stub,
            kind=stub.kind.value,
            current_containers=result.current_containers,
            pending_containers=result.pending_containers,
            desired_containers=result.desired_containers,
            signal_name=result.signal_name,
            signal_value=result.signal_value,
            max_containers=plan.max_containers,
            pressure_saturated=_pressure_saturated(result.signal_value, plan),
            failed_container_count=len(result.failed_containers),
            decision=result.decision.value,
            reason=result.reason,
            guardrails=result.guardrails,
            actions=action_payloads,
        )
        _record_autoscaler_state(
            self.services,
            source=identity.source,
            target_kind=identity.kind,
            stub=stub,
            current_count=result.current_containers,
            desired_count=result.desired_containers,
            signal_name=result.signal_name,
            signal_value=result.signal_value,
            decision=result.decision.value,
            reason=result.reason,
            active=result.active,
            valid=result.valid,
            lock_acquired=result.lock_acquired,
            owner_lock_key=self._lock_key(stub),
            failed_container_count=len(result.failed_containers),
            last_sample={
                result.signal_name: result.signal_value,
                "current_containers": result.current_containers,
                "pending_containers": result.pending_containers,
                "guardrails": result.guardrails,
            },
            last_actions=action_payloads,
        )

    def _record_lock_contention(self, stub: StubRecord, lock_key: str) -> AutoscaleResult:
        """A tick that found the lock held is still a tick that looked.

        Left unrecorded it reads as an autoscaler that never ran, which is the
        one reading that sends somebody looking at the wrong process.
        """

        identity = self.workload.identity
        result = AutoscaleResult(
            kind=identity.kind,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            signal_name=identity.signal_name,
            decision=ScaleDecisionKind.Hold,
            reason="autoscaler lock already held",
            lock_acquired=False,
        )
        _record_autoscaler_state(
            self.services,
            source=identity.source,
            target_kind=identity.kind,
            stub=stub,
            current_count=result.current_containers,
            desired_count=result.desired_containers,
            signal_name=result.signal_name,
            decision=result.decision.value,
            reason=result.reason,
            active=result.active,
            valid=result.valid,
            lock_acquired=result.lock_acquired,
            owner_lock_key=lock_key,
        )
        return result

    def _lock_key(self, stub: StubRecord) -> str:
        return self.redis.key(
            "autoscaling",
            self.workload.identity.lock_namespace,
            stub.workspace_id,
            stub.id,
            "lock",
        )

    def _acquire_lock(self, key: str, token: str) -> bool:
        return bool(self.redis.set(key, token, nx=True, ex=AUTOSCALER_LOCK_TTL_SECONDS))

    def _release_lock(self, key: str, token: str) -> None:
        if _redis_text(self.redis.get(key)) == token:
            self.redis.delete(key)


@dataclass(slots=True)
class FunctionAutoscaler:
    """Give a function's backlog enough containers to be worked through.

    The only thing that provisions a function past its first container. An
    invocation may bring an idle stub up so a cold call does not wait for a
    tick, and stops there; everything about depth is decided here, from the
    whole backlog, once per tick per stub.

    Scaling down is deliberately not done: a function container ends itself when
    its keep-warm window passes with no work, so the way to have fewer is to
    stop giving them any. Stopping one from outside risks taking an invocation
    with it.
    """

    services: SchedulerServices
    functions: FunctionAutoscaleControl

    @property
    def identity(self) -> AutoscalerIdentity:
        return FUNCTION_AUTOSCALER

    def selects(self, stub: StubRecord) -> bool:
        # Every function stub, bound to a deployment or not. A stub reached by
        # `.remote()`, `.map()` or `lazycloud run` before anything is deployed
        # has a backlog like any other, and it is the one case where the first
        # container came from an invocation rather than from here — so refusing
        # it leaves a fan-out being served one container at a time.
        return stub.kind is StubKind.Function

    def sample(self, stub: StubRecord) -> int:
        return self.functions.unclaimed_task_count(stub.id)

    def plan(self, stub: StubRecord, *, signal: int, current: int) -> ScalePlan:
        config = _function_autoscaler_config(stub.config)
        decision = decide_backlog_scale(
            BacklogAutoscalerSample(
                queue_length=signal,
                running_tasks=0,
                current_containers=current,
            ),
            config,
        )
        return ScalePlan(
            desired_containers=decision.desired_containers,
            reason=decision.reason.value,
            decision=decision.decision,
            valid=decision.valid,
            max_containers=config.effective_max_containers,
            min_containers=config.min_containers,
            tasks_per_container=config.tasks_per_container,
        )

    def start_one(self, stub: StubRecord) -> str | None:
        # The container is reserved against the stub, so its id is settled where
        # the ceiling is checked rather than here.
        return "" if self.functions.start_function_container(stub.id) else None

    def scale_down(
        self,
        stub: StubRecord,
        containers: list[ContainerRecord],
        count: int,
        *,
        scheduler_statuses: Mapping[str, SchedulerContainerStatus],
        active_instance: bool,
        now: datetime,
    ) -> list[AutoscaleAction]:
        """Remove containers only where nothing else will.

        A function container ends itself when its idle window passes, so where
        there is a window the way to have fewer is to stop giving them work —
        stopping one early would throw away exactly the warm container the next
        call was going to reach. A stub with a warm floor has no window to wait
        for, and then this is the only thing that can bring the count down.

        Busy containers are left alone. Stopping one settles what it holds by
        releasing it, so no invocation is lost, but the part that had already
        run is, and a handler that is not idempotent would run it twice.
        """

        del scheduler_statuses, active_instance, now
        if stub.config.runtime.keep_warm >= 0:
            return []
        # Every container the excess was counted from, starting ones included.
        # Two schedulers provisioning the same floor leave the newest half still
        # pending, and considering only the running ones would answer an excess
        # of two by stopping the two that are warm and keeping the two that are
        # not — reclaiming the floor by discarding exactly what it is for.
        busy = self.functions.containers_holding_work([item.id for item in containers])
        idle = [container for container in containers if container.id not in busy]
        idle.sort(key=lambda container: container.created_at, reverse=True)
        actions: list[AutoscaleAction] = []
        for container in idle[:count]:
            stopped = self.services.containers.stop(
                container.id,
                reason=StopContainerReason.Scheduler,
            )
            actions.append(
                AutoscaleAction(
                    container_id=stopped.id,
                    action="stop",
                    reason="held container count is above the warm floor",
                )
            )
        return actions


@dataclass(slots=True)
class EndpointAutoscaler:
    """Scale an endpoint on the dispatches it is already serving."""

    services: SchedulerServices
    endpoints: EndpointAutoscaleControl
    dispatches: EndpointAutoscalingDispatchReader

    @property
    def identity(self) -> AutoscalerIdentity:
        return ENDPOINT_AUTOSCALER

    def selects(self, stub: StubRecord) -> bool:
        return stub.kind in {StubKind.Endpoint, StubKind.Asgi} and bool(stub.deployment_id)

    def sample(self, stub: StubRecord) -> int:
        return self.dispatches.active_count(stub.id)

    def plan(self, stub: StubRecord, *, signal: int, current: int) -> ScalePlan:
        config = _endpoint_autoscaler_config(stub.config)
        desired, reason = _endpoint_desired_containers(active_requests=signal, config=config)
        return ScalePlan(
            desired_containers=desired,
            reason=reason,
            decision=scale_kind(desired, current),
            max_containers=config.max_containers,
            min_containers=config.min_containers,
            tasks_per_container=config.tasks_per_container,
        )

    def start_one(self, stub: StubRecord) -> str | None:
        response = self.endpoints.start_endpoint_serve(
            StartEndpointServeRequest(
                stub_id=stub.id,
                timeout=_endpoint_timeout_seconds(stub.config),
            )
        )
        return response.container_id

    def scale_down(
        self,
        stub: StubRecord,
        containers: list[ContainerRecord],
        count: int,
        *,
        scheduler_statuses: Mapping[str, SchedulerContainerStatus],
        active_instance: bool,
        now: datetime,
    ) -> list[AutoscaleAction]:
        del active_instance
        actions: list[AutoscaleAction] = []
        for container in _stoppable_endpoint_containers(
            self.dispatches,
            stub,
            containers,
            scheduler_statuses,
            keep_warm_seconds=_endpoint_autoscaler_config(stub.config).keep_warm_seconds,
            now=now,
        ):
            if len(actions) >= count:
                break
            stopped = self.services.containers.stop(
                container.id,
                reason=StopContainerReason.Scheduler,
            )
            actions.append(
                AutoscaleAction(
                    container_id=stopped.id,
                    action="stop",
                    reason="endpoint request pressure no longer requires container",
                )
            )
        return actions


@dataclass(slots=True)
class PodAutoscaler:
    """Scale a pod deployment on the connections held against it."""

    services: SchedulerServices
    redis: RedisClient
    pods: PodControl

    @property
    def identity(self) -> AutoscalerIdentity:
        return POD_AUTOSCALER

    def selects(self, stub: StubRecord) -> bool:
        return stub.kind in {StubKind.Pod, StubKind.Sandbox} and (
            stub.kind is StubKind.Sandbox or bool(stub.deployment_id)
        )

    def sample(self, stub: StubRecord) -> int:
        return _pod_total_connections(self.redis, stub.workspace_id, stub.id)

    def plan(self, stub: StubRecord, *, signal: int, current: int) -> ScalePlan:
        config = _pod_autoscaler_config(stub)
        decision = decide_pod_scale(
            PodAutoscalerSample(current_containers=current, total_connections=signal),
            config,
        )
        return ScalePlan(
            desired_containers=decision.desired_containers,
            reason=decision.reason.value,
            decision=decision.decision,
            valid=decision.valid,
            max_containers=config.max_containers,
            min_containers=config.min_containers,
        )

    def start_one(self, stub: StubRecord) -> str | None:
        response = self.pods.create_pod(CreatePodRequest(stub_id=stub.id))
        if not response.container_id:
            # A pod that reports no container is a failure to record, not a
            # refusal to respect: nothing was started and nothing named it.
            raise InvalidInputError("pod create returned no container id")
        return response.container_id

    def scale_down(
        self,
        stub: StubRecord,
        containers: list[ContainerRecord],
        count: int,
        *,
        scheduler_statuses: Mapping[str, SchedulerContainerStatus],
        active_instance: bool,
        now: datetime,
    ) -> list[AutoscaleAction]:
        del scheduler_statuses
        workspace_id = stub.workspace_id
        # A sandbox is held by its lock rather than by a window, which is the
        # same distinction `keep_warm_lock_authoritative` states below.
        keep_warm_seconds = (
            0 if stub.kind is StubKind.Sandbox else _pod_autoscaler_config(stub).keep_warm_seconds
        )
        states = _pod_container_states(self.redis, workspace_id, stub, containers)
        stop_plan = select_stoppable_pod_containers(
            states,
            active_instance=active_instance,
            keep_warm_seconds=keep_warm_seconds,
            keep_warm_lock_authoritative=stub.kind is StubKind.Sandbox,
            now_seconds=int(now.timestamp()),
        )
        actions: list[AutoscaleAction] = []
        for container_id in stop_plan.stoppable_container_ids[:count]:
            stopped = self.services.containers.stop(
                container_id,
                reason=StopContainerReason.Scheduler,
            )
            self.redis.delete(
                self.redis.key(pod_keep_warm_lock_key(workspace_id, stub.id, container_id))
            )
            actions.append(
                AutoscaleAction(
                    container_id=stopped.id,
                    action="stop",
                    reason="pod deployment desired capacity no longer requires container",
                )
            )
        return actions


def _autoscaling_enabled(stub: StubRecord) -> bool:
    raw = stub.config.metadata.get("autoscaling_enabled", True)
    return raw is not False


def _function_autoscaler_config(config: StubConfig) -> BacklogAutoscalerConfig:
    autoscaler = config.autoscaler
    return BacklogAutoscalerConfig(
        tasks_per_container=autoscaler.tasks_per_container,
        min_containers=autoscaler.min_containers,
        max_containers=function_container_ceiling(autoscaler.max_containers),
    )


def _pressure_saturated(signal: int, plan: ScalePlan) -> bool:
    """Whether the workload wants more than its ceiling can ever serve.

    One formula for all three: the ceiling times what one container takes is
    what the stub can serve at most, and a signal above that is pressure the
    autoscaler is not allowed to answer.
    """

    servable = max(plan.max_containers, 0) * max(plan.tasks_per_container, 1)
    return servable > 0 and signal > servable


class EndpointAutoscalerConfig(ContractModel):
    min_containers: int = 0
    max_containers: int = 1
    tasks_per_container: int = 1
    keep_warm_seconds: int = 0


def _record_autoscaler_metrics(
    services: SchedulerServices,
    *,
    source: str,
    stub: StubRecord,
    kind: str,
    current_containers: int,
    pending_containers: int,
    desired_containers: int,
    signal_name: str,
    signal_value: int,
    max_containers: int,
    pressure_saturated: bool,
    failed_container_count: int,
    decision: str,
    reason: str,
    guardrails: dict[str, JsonValue],
    actions: list[dict[str, JsonValue]],
) -> None:
    base_labels = {
        "source": source,
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": kind,
    }
    services.metrics.increment(
        "autoscaler_decisions_total",
        labels={**base_labels, "decision": decision, "reason": reason},
    )
    services.metrics.set_gauge(
        "autoscaler_current_containers",
        current_containers,
        labels=base_labels,
    )
    services.metrics.set_gauge(
        "autoscaler_desired_containers",
        desired_containers,
        labels=base_labels,
    )
    services.metrics.set_gauge(
        "autoscaler_pending_containers",
        pending_containers,
        labels=base_labels,
    )
    services.metrics.set_gauge(
        "autoscaler_idle_containers",
        max(current_containers - desired_containers, 0),
        labels=base_labels,
    )
    services.metrics.set_gauge(
        "autoscaler_failed_containers",
        failed_container_count,
        labels=base_labels,
    )
    services.metrics.set_gauge(
        "autoscaler_failed_container_threshold_reached",
        1 if reason == "failed container threshold reached" else 0,
        labels=base_labels,
    )
    services.metrics.set_gauge(
        "autoscaler_signal",
        signal_value,
        labels={**base_labels, "signal": signal_name},
    )
    services.metrics.set_gauge(
        "autoscaler_max_containers",
        max_containers,
        labels=base_labels,
    )
    services.metrics.set_gauge(
        "autoscaler_pressure_saturated",
        1 if pressure_saturated else 0,
        labels={**base_labels, "signal": signal_name},
    )
    throttled = _guardrail_limited(guardrails)
    services.metrics.set_gauge(
        "autoscaler_guardrail_throttled",
        1 if throttled else 0,
        labels=base_labels,
    )
    if throttled:
        services.metrics.increment(
            "autoscaler_throttled_total",
            labels={**base_labels, "reason": _guardrail_reason(guardrails)},
        )
    no_worker_capacity = False
    for action in actions:
        action_name = _action_name(action)
        if not action_name:
            continue
        services.metrics.increment(
            "autoscaler_actions_total",
            labels={**base_labels, "action": action_name},
        )
        if "failed" in action_name:
            services.metrics.increment(
                "autoscaler_scale_failures_total",
                labels={**base_labels, "action": action_name},
            )
            if _is_no_worker_capacity(_action_reason(action)):
                no_worker_capacity = True
                services.metrics.increment(
                    "autoscaler_no_worker_capacity_total",
                    labels=base_labels,
                )
    services.metrics.set_gauge(
        "autoscaler_no_worker_capacity",
        1 if no_worker_capacity else 0,
        labels=base_labels,
    )


def _autoscale_action_payloads(
    actions: Sequence[AutoscaleAction],
) -> list[dict[str, JsonValue]]:
    return [
        {
            "container_id": action.container_id,
            "action": action.action,
            "reason": action.reason,
        }
        for action in actions
    ]


def _guardrail_limited(guardrails: dict[str, JsonValue]) -> bool:
    return guardrails.get("limited") is True


def _guardrail_reason(guardrails: dict[str, JsonValue]) -> str:
    reason = guardrails.get("reason")
    return reason if isinstance(reason, str) and reason else "guardrail-limited"


def _action_name(action: dict[str, JsonValue]) -> str:
    value = action.get("action")
    return value if isinstance(value, str) else ""


def _action_reason(action: dict[str, JsonValue]) -> str:
    value = action.get("reason")
    return value if isinstance(value, str) else ""


def _is_no_worker_capacity(reason: str) -> bool:
    normalized = reason.lower()
    return "no worker capacity" in normalized or "worker capacity" in normalized


def _should_persist_scale_event(
    services: SchedulerServices,
    *,
    target_kind: AutoscalerTargetKind,
    stub: StubRecord,
    decision: str,
    desired_count: int,
    reason: str,
    valid: bool,
    actions_taken: bool,
) -> bool:
    """Persist scale decisions only on transitions.

    The autoscalers run every tick; steady-state repeats of the same decision
    are represented by the durable autoscaler state record and the decision
    metrics, so only ticks that acted or changed the decision produce an event
    row.
    """
    if actions_taken:
        return True
    previous = services.autoscaler_states.get(
        workspace_id=stub.workspace_id,
        target_kind=target_kind,
        target_id=stub.id,
    )
    if previous is None:
        return True
    return (
        previous.decision != decision
        or previous.desired_count != desired_count
        or previous.reason != reason
        or previous.valid != valid
    )


def _record_autoscaler_state(
    services: SchedulerServices,
    *,
    source: str,
    target_kind: AutoscalerTargetKind,
    stub: StubRecord,
    decision: str,
    reason: str,
    current_count: int = 0,
    desired_count: int = 0,
    signal_name: str = "",
    signal_value: int = 0,
    active: bool = True,
    valid: bool = True,
    lock_acquired: bool = True,
    owner_lock_key: str = "",
    failed_container_count: int = 0,
    last_sample: dict[str, JsonValue] | None = None,
    last_actions: list[dict[str, JsonValue]] | None = None,
    cooldown_until: datetime | None = None,
) -> None:
    actions = last_actions or []
    state = AutoscalerStateRecord(
        name=autoscaler_state_name(target_kind, stub.id),
        workspace_id=stub.workspace_id,
        source=source,
        target_kind=target_kind,
        target_id=stub.id,
        deployment_id=stub.deployment_id or "",
        app_id=stub.app_id or "",
        current_count=current_count,
        desired_count=desired_count,
        signal_name=signal_name,
        signal_value=signal_value,
        decision=decision,
        reason=reason,
        active=active,
        valid=valid,
        lock_acquired=lock_acquired,
        owner_lock_key=owner_lock_key,
        cooldown_until=cooldown_until,
        failed_container_count=failed_container_count,
        error=_autoscaler_error(actions),
        last_sample=last_sample or {},
        last_actions=actions,
        updated_at=utc_now(),
    )
    services.autoscaler_states.upsert(state)


def _autoscaler_error(actions: list[dict[str, JsonValue]]) -> str:
    for action in actions:
        action_name = action.get("action")
        if isinstance(action_name, str) and "failed" in action_name:
            reason = action.get("reason")
            return reason if isinstance(reason, str) else action_name
    return ""


def _deployment_active(services: SchedulerServices, stub: StubRecord) -> bool:
    if stub.app_id:
        try:
            app = services.apps.get(stub.app_id, workspace=stub.workspace_id)
        except NotFoundError:
            return False
        if not app.active:
            return False
    if not stub.deployment_id:
        return True
    try:
        deployment = services.deployments.get(stub.deployment_id)
    except NotFoundError:
        return False
    return deployment.active and deployment.deleted_at is None


def _containers_for_stub(
    services: SchedulerServices,
    stub: StubRecord,
) -> list[ContainerRecord]:
    containers = services.containers.list(workspace_id=stub.workspace_id)
    return [container for container in containers if container.stub_id == stub.id]


def _active_containers(containers: list[ContainerRecord]) -> list[ContainerRecord]:
    return [
        container
        for container in containers
        if container.status in {ContainerStatus.Pending, ContainerStatus.Running}
    ]


def _partition_backed_containers(
    containers: list[ContainerRecord],
    scheduler_statuses: Mapping[str, SchedulerContainerStatus],
    requests: ContainerRequestReader,
    *,
    now: datetime,
) -> tuple[list[ContainerRecord], list[StaleContainer]]:
    """Split records the scheduler still backs from records that only look live.

    The durable row is what the ceiling is counted from, so a row that says
    `pending` or `running` while nothing is going to make it true is a slot held
    against a workload that cannot use it. At `max_containers = 1` that is not a
    degradation, it is a stop: desired equals current forever and the backlog
    grows one entry per fire.
    """

    live: list[ContainerRecord] = []
    stale: list[StaleContainer] = []
    for container in _active_containers(containers):
        reason = _stale_reason(
            container,
            scheduler_statuses.get(container.id),
            requests,
            now=now,
        )
        if reason:
            stale.append(StaleContainer(record=container, reason=reason))
            continue
        live.append(container)
    return live, stale


def _stale_reason(
    container: ContainerRecord,
    scheduler_status: SchedulerContainerStatus | None,
    requests: ContainerRequestReader,
    *,
    now: datetime,
) -> str:
    """Why this record is not capacity, or empty where it still is.

    Read from the durable row and from what actually holds the container, never
    from whether a Redis key happens to be alive: the scheduler state is
    re-armed by whoever holds it, so a worker wedged half-way through a start
    refreshes it indefinitely, and a durable row that only becomes true when a
    cache entry expires has the ownership backwards.

    A `running` row is the case pods already covered: it has started, so no
    scheduler state at all means the worker that was running it is gone.

    A `pending` row is the one that stranded functions and endpoints, and it has
    two legitimate reasons to still be pending. Either a request for it is
    queued, in which case the dispatcher owns it and bounds its own retrying —
    read here rather than guessed at, so the deadline below never has to cover
    a wait for capacity. Or a worker has taken it and is starting it, which is
    what the deadline covers and the only thing it covers.
    """

    if scheduler_status is not None and scheduler_status not in {
        SchedulerContainerStatus.Pending,
        SchedulerContainerStatus.Running,
    }:
        return f"scheduler state is {scheduler_status.value}"
    if container.status is ContainerStatus.Running:
        return "scheduler state missing for running container" if scheduler_status is None else ""
    if scheduler_status is SchedulerContainerStatus.Running:
        # Started, and only the durable row has yet to catch up.
        return ""
    if requests.has_recoverable_container_request(
        container.id,
        worker_id=container.runtime_worker_id,
    ):
        return ""
    if (now - container.created_at).total_seconds() < CONTAINER_START_DEADLINE_SECONDS:
        return ""
    return "container never started within the start deadline"


def _pending_container_count(containers: list[ContainerRecord]) -> int:
    return sum(1 for container in containers if container.status is ContainerStatus.Pending)


def _recent_failed_container_ids(
    containers: list[ContainerRecord],
    *,
    now: datetime,
    window_seconds: int,
) -> list[str]:
    cutoff = now - timedelta(seconds=max(window_seconds, 0))
    failed: list[ContainerRecord] = []
    for container in containers:
        if container.status is not ContainerStatus.Failed:
            continue
        failed_at = container.finished_at or container.started_at or container.created_at
        if failed_at < cutoff:
            continue
        failed.append(container)
    failed.sort(
        key=lambda container: container.finished_at or container.started_at or container.created_at,
        reverse=True,
    )
    return [container.id for container in failed]


def _stoppable_endpoint_containers(
    dispatches: EndpointAutoscalingDispatchReader,
    stub: StubRecord,
    containers: list[ContainerRecord],
    scheduler_statuses: Mapping[str, SchedulerContainerStatus],
    *,
    keep_warm_seconds: int,
    now: datetime,
) -> list[ContainerRecord]:
    dispatch_records = dispatches.list_by_stub(stub.id)
    candidates = [
        container
        for container in containers
        if container.status is ContainerStatus.Running
        and scheduler_statuses.get(container.id) is not SchedulerContainerStatus.Stopping
        and not _endpoint_container_has_active_dispatch(dispatch_records, container.id)
        and _endpoint_container_keep_warm_elapsed(
            dispatch_records,
            container,
            keep_warm_seconds=keep_warm_seconds,
            now=now,
        )
    ]
    candidates.sort(key=lambda container: container.created_at, reverse=True)
    return candidates


def _endpoint_container_has_active_dispatch(
    dispatch_records: list[EndpointAutoscalingDispatchObservation],
    container_id: str,
) -> bool:
    for record in dispatch_records:
        if record.container_id != container_id:
            continue
        if record.active:
            return True
    return False


def _endpoint_container_keep_warm_elapsed(
    dispatch_records: list[EndpointAutoscalingDispatchObservation],
    container: ContainerRecord,
    *,
    keep_warm_seconds: int,
    now: datetime,
) -> bool:
    if keep_warm_seconds <= 0:
        return True
    latest = container.started_at or container.created_at
    for record in dispatch_records:
        if record.container_id != container.id:
            continue
        finished_at = record.finished_at
        if finished_at is not None and finished_at > latest:
            latest = finished_at
    return (now - latest).total_seconds() >= keep_warm_seconds


def _pod_total_connections(redis: RedisClient, workspace_id: str, stub_id: str) -> int:
    return _redis_non_negative_int(
        redis.get(redis.key(pod_total_connections_key(workspace_id, stub_id)))
    )


def _pod_container_states(
    redis: RedisClient,
    workspace_id: str,
    stub: StubRecord,
    containers: list[ContainerRecord],
) -> list[PodContainerState]:
    states: list[PodContainerState] = []
    for container in sorted(containers, key=lambda item: item.created_at, reverse=True):
        states.append(
            PodContainerState(
                container_id=container.id,
                status=container.status,
                started_at_seconds=int((container.started_at or container.created_at).timestamp()),
                keep_warm_lock_present=_exists(
                    redis,
                    pod_keep_warm_lock_key(workspace_id, stub.id, container.id),
                ),
                active_connections=_redis_non_negative_int(
                    redis.get(
                        redis.key(
                            pod_container_connections_key(
                                workspace_id,
                                stub.id,
                                container.id,
                            )
                        )
                    )
                ),
            )
        )
    return states


def _endpoint_autoscaler_config(config: StubConfig) -> EndpointAutoscalerConfig:
    autoscaler = config.autoscaler
    runtime_config = config.runtime
    return EndpointAutoscalerConfig(
        min_containers=autoscaler.min_containers,
        max_containers=autoscaler.max_containers,
        tasks_per_container=autoscaler.tasks_per_container,
        keep_warm_seconds=max(runtime_config.keep_warm, 0),
    )


def _pod_autoscaler_config(stub: StubRecord) -> PodAutoscalerConfig:
    autoscaler = stub.config.autoscaler
    runtime_config = stub.config.runtime
    keep_warm_seconds = max(runtime_config.keep_warm, -1)
    min_containers = autoscaler.min_containers
    if keep_warm_seconds == -1:
        min_containers = max(min_containers, 1)
    return PodAutoscalerConfig(
        stub_type=(
            PodStubType.Sandbox if stub.kind is StubKind.Sandbox else PodStubType.PodDeployment
        ),
        min_containers=min_containers,
        max_containers=autoscaler.max_containers,
        keep_warm_seconds=keep_warm_seconds,
    )


def _endpoint_desired_containers(
    *,
    active_requests: int,
    config: EndpointAutoscalerConfig,
) -> tuple[int, str]:
    if active_requests < 0:
        return 0, "invalid sample"
    if active_requests == 0:
        return config.min_containers, "endpoint idle"
    required = _ceil_div(active_requests, config.tasks_per_container)
    desired = min(max(required, config.min_containers), config.max_containers)
    reason = "replica limit reached" if desired < required else "endpoint requests active"
    return desired, reason


def _failed_container_threshold(config: StubConfig) -> int:
    return _first_configured_non_negative(
        config.autoscaler.failed_container_threshold,
        config.autoscaler.max_failed_containers,
        config.autoscaler.failure_threshold,
        default=AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD,
    )


def _failed_container_window_seconds(config: StubConfig) -> int:
    return _first_configured_non_negative(
        config.autoscaler.failed_container_window_seconds,
        config.autoscaler.failure_window_seconds,
        default=AUTOSCALER_DEFAULT_FAILURE_WINDOW_SECONDS,
    )


def _endpoint_timeout_seconds(config: StubConfig) -> int:
    return (
        int(config.task_policy.timeout_seconds)
        or int(config.task_policy.timeout)
        or int(config.runtime.timeout_seconds or 0)
        or ENDPOINT_AUTOSCALER_DEFAULT_TIMEOUT_SECONDS
    )


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // max(denominator, 1))


def _first_configured_non_negative(
    *values: int | None,
    default: int,
) -> int:
    for value in values:
        if value is not None:
            return max(value, 0)
    return default


def _exists(redis: RedisClient, logical_key: str) -> bool:
    return int(redis.exists(_redis_key(redis, logical_key)) or 0) > 0


def _redis_key(redis: RedisClient, logical_key: str) -> str:
    return redis.key(logical_key)


def _redis_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return "" if value is None else str(value)


def _redis_non_negative_int(value: object) -> int:
    try:
        return max(int(_redis_text(value)), 0)
    except ValueError:
        return 0


__all__ = [
    "CONTAINER_START_DEADLINE_SECONDS",
    "AutoscaleAction",
    "AutoscaleResult",
    "AutoscalerIdentity",
    "AutoscalingDriver",
    "ContainerRequestReader",
    "EndpointAutoscaler",
    "FunctionAutoscaler",
    "PodAutoscaler",
    "ScalePlan",
    "SchedulerContainerStateReader",
    "SchedulerServices",
    "StaleContainer",
    "WorkloadAutoscaler",
]
