from __future__ import annotations

from collections.abc import Sequence
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
from shared.errors import DomainError, NotFoundError
from shared.http.endpoints import StartEndpointServeRequest, StartEndpointServeResponse
from shared.http.pods import CreatePodRequest, CreatePodResponse
from shared.scheduling import SchedulerContainerState, SchedulerContainerStatus
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
FUNCTION_AUTOSCALER_SOURCE = "function.autoscaler"
ENDPOINT_AUTOSCALER_DEFAULT_TIMEOUT_SECONDS = 600
ENDPOINT_AUTOSCALER_SOURCE = "endpoint.autoscaler"
POD_AUTOSCALER_SOURCE = "pod.autoscaler"


class PodControl(Protocol):
    def create_pod(self, request: CreatePodRequest) -> CreatePodResponse: ...

    def expire_pods(self, *, now: datetime | None = None) -> list[ContainerRecord]: ...


class PodContainerStateReader(Protocol):
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...


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


class AutoscaleResult(ContractModel):
    """What one tick concluded about one workload.

    `kind` is what separates the three, and the sample is carried as a name and
    a number rather than a field per kind. A result shaped per kind meant every
    reader of the autoscaler surface — the state row, the metrics, the history,
    the operator's reconcile output — had a branch for each, and the kind added
    last is the one each of those branches forgot.
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
    keep_warm_seconds: int = 0
    saturated: bool = False


class WorkloadAutoscaler(Protocol):
    """The part of autoscaling that is genuinely per kind.

    A function scales on backlog depth, an endpoint on in-flight dispatches, a
    pod on connections; they start containers three different ways and disagree
    about which of them may be stopped. That is the whole of it. Deliberately no
    reach into the pass that runs around this: what a workload cannot see, it
    cannot forget to do.
    """

    @property
    def identity(self) -> AutoscalerIdentity: ...

    def selects(self, stub: StubRecord) -> bool: ...

    def partition(
        self,
        containers: list[ContainerRecord],
    ) -> tuple[list[ContainerRecord], list[ContainerRecord]]:
        """Split what is holding capacity from what only claims to be."""
        ...

    def recover(self, stale: list[ContainerRecord]) -> list[AutoscaleAction]: ...

    def sample(self, stub: StubRecord) -> int: ...

    def plan(self, stub: StubRecord, *, signal: int, current: int) -> ScalePlan: ...

    def scale_up(self, stub: StubRecord, count: int) -> list[AutoscaleAction]: ...

    def scale_down(
        self,
        stub: StubRecord,
        containers: list[ContainerRecord],
        count: int,
        *,
        keep_warm_seconds: int,
        active_instance: bool,
        now: datetime,
    ) -> list[AutoscaleAction]: ...


@dataclass(slots=True)
class AutoscalingDriver:
    """The reconcile pass every workload gets, identically.

    Stub selection and the pause an operator set, the stub's lock and the state
    a contended tick still records, the container counts, the failed-container
    threshold, the inactive deployment, the workspace guardrail, and the metrics
    and event and state row a tick leaves behind: all of it here, once, for
    every kind.

    Written as three copies these drifted, and the drift was invisible because
    each copy was individually correct. The function autoscaler was the newest
    copy and it had never read the pause flag, never recorded a metric, and
    never named an action it took — three ways of being absent from the surface
    an operator uses, none of which looked like a bug in a diff.
    """

    services: SchedulerServices
    redis: RedisClient
    workload: WorkloadAutoscaler

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
        holding, stale = self.workload.partition(containers)
        actions = self.workload.recover(stale)
        current = len(holding)
        pending = _pending_container_count(containers)
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
            actions.extend(self.workload.scale_up(stub, delta))
        elif delta < 0:
            actions.extend(
                self.workload.scale_down(
                    stub,
                    holding,
                    -delta,
                    keep_warm_seconds=plan.keep_warm_seconds,
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
                "actions": _autoscale_action_json_values(result.actions),
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
            pressure_saturated=plan.saturated,
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
        # Bound to a deployment, as the other two workloads also require. A
        # deploy leaves behind a stub with no deployment id, and provisioning
        # from that one would hold a second copy of everything this stub is
        # configured to hold — invisible while a function held containers only
        # for work it had, and a doubled bill once it holds a warm floor.
        return stub.kind is StubKind.Function and bool(stub.deployment_id)

    def partition(
        self,
        containers: list[ContainerRecord],
    ) -> tuple[list[ContainerRecord], list[ContainerRecord]]:
        return _active_containers(containers), []

    def recover(self, stale: list[ContainerRecord]) -> list[AutoscaleAction]:
        del stale
        return []

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
            keep_warm_seconds=stub.config.runtime.keep_warm,
            saturated=_backlog_pressure_saturated(signal, config),
        )

    def scale_up(self, stub: StubRecord, count: int) -> list[AutoscaleAction]:
        actions: list[AutoscaleAction] = []
        for _ in range(count):
            try:
                started = self.functions.start_function_container(stub.id)
            except DomainError as exc:
                actions.append(AutoscaleAction(action="scale-up-failed", reason=exc.message))
                break
            if not started:
                break
            actions.append(AutoscaleAction(action="scale-up"))
        return actions

    def scale_down(
        self,
        stub: StubRecord,
        containers: list[ContainerRecord],
        count: int,
        *,
        keep_warm_seconds: int,
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

        del active_instance, now
        if keep_warm_seconds >= 0:
            return []
        candidates = [
            container for container in containers if container.status is ContainerStatus.Running
        ]
        busy = self.functions.containers_holding_work([item.id for item in candidates])
        idle = [container for container in candidates if container.id not in busy]
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
    redis: RedisClient
    endpoints: EndpointAutoscaleControl
    dispatches: EndpointAutoscalingDispatchReader

    @property
    def identity(self) -> AutoscalerIdentity:
        return ENDPOINT_AUTOSCALER

    def selects(self, stub: StubRecord) -> bool:
        return stub.kind in {StubKind.Endpoint, StubKind.Asgi} and bool(stub.deployment_id)

    def partition(
        self,
        containers: list[ContainerRecord],
    ) -> tuple[list[ContainerRecord], list[ContainerRecord]]:
        return _active_containers(containers), []

    def recover(self, stale: list[ContainerRecord]) -> list[AutoscaleAction]:
        del stale
        return []

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
            keep_warm_seconds=config.keep_warm_seconds,
            saturated=_endpoint_pressure_saturated(signal, config),
        )

    def scale_up(self, stub: StubRecord, count: int) -> list[AutoscaleAction]:
        actions: list[AutoscaleAction] = []
        timeout = _endpoint_timeout_seconds(stub.config)
        for _ in range(count):
            try:
                response = self.endpoints.start_endpoint_serve(
                    StartEndpointServeRequest(stub_id=stub.id, timeout=timeout)
                )
            except DomainError as exc:
                actions.append(AutoscaleAction(action="scale-up-failed", reason=exc.message))
                break
            actions.append(
                AutoscaleAction(
                    container_id=response.container_id,
                    action="start",
                    reason="endpoint request pressure requires more containers",
                )
            )
        return actions

    def scale_down(
        self,
        stub: StubRecord,
        containers: list[ContainerRecord],
        count: int,
        *,
        keep_warm_seconds: int,
        active_instance: bool,
        now: datetime,
    ) -> list[AutoscaleAction]:
        del active_instance
        actions: list[AutoscaleAction] = []
        for container in _stoppable_endpoint_containers(
            self.dispatches,
            self.redis,
            stub,
            containers,
            keep_warm_seconds=keep_warm_seconds,
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
    container_states: PodContainerStateReader | None = None

    @property
    def identity(self) -> AutoscalerIdentity:
        return POD_AUTOSCALER

    def selects(self, stub: StubRecord) -> bool:
        return stub.kind in {StubKind.Pod, StubKind.Sandbox} and (
            stub.kind is StubKind.Sandbox or bool(stub.deployment_id)
        )

    def partition(
        self,
        containers: list[ContainerRecord],
    ) -> tuple[list[ContainerRecord], list[ContainerRecord]]:
        return _pod_active_containers(containers, self.container_states)

    def recover(self, stale: list[ContainerRecord]) -> list[AutoscaleAction]:
        actions: list[AutoscaleAction] = []
        for container in stale:
            stopped = self.services.containers.stop(
                container.id,
                reason=StopContainerReason.Scheduler,
            )
            actions.append(
                AutoscaleAction(
                    container_id=stopped.id,
                    action="recover-stale",
                    reason="scheduler state missing for running pod",
                )
            )
        return actions

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
            keep_warm_seconds=(0 if stub.kind is StubKind.Sandbox else config.keep_warm_seconds),
            saturated=_pod_pressure_saturated(signal, config),
        )

    def scale_up(self, stub: StubRecord, count: int) -> list[AutoscaleAction]:
        actions: list[AutoscaleAction] = []
        for _ in range(count):
            try:
                response = self.pods.create_pod(CreatePodRequest(stub_id=stub.id))
            except DomainError as exc:
                actions.append(AutoscaleAction(action="scale-up-failed", reason=str(exc)))
                break
            if not response.container_id:
                actions.append(
                    AutoscaleAction(
                        action="scale-up-failed",
                        reason="pod create returned no container id",
                    )
                )
                break
            actions.append(
                AutoscaleAction(
                    container_id=response.container_id,
                    action="start",
                    reason="pod deployment desired capacity requires more containers",
                )
            )
        return actions

    def scale_down(
        self,
        stub: StubRecord,
        containers: list[ContainerRecord],
        count: int,
        *,
        keep_warm_seconds: int,
        active_instance: bool,
        now: datetime,
    ) -> list[AutoscaleAction]:
        workspace_id = stub.workspace_id
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


def _backlog_pressure_saturated(queue_length: int, config: BacklogAutoscalerConfig) -> bool:
    servable = max(config.effective_max_containers, 0) * max(config.tasks_per_container, 1)
    return servable > 0 and queue_length > servable


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


def _autoscale_action_json_values(actions: Sequence[AutoscaleAction]) -> list[JsonValue]:
    return [payload for payload in _autoscale_action_payloads(actions)]


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


def _endpoint_pressure_saturated(
    active_requests: int,
    config: EndpointAutoscalerConfig,
) -> bool:
    target_requests = max(config.max_containers, 0) * max(config.tasks_per_container, 1)
    return target_requests > 0 and active_requests > target_requests


def _pod_pressure_saturated(total_connections: int, config: PodAutoscalerConfig) -> bool:
    return config.max_containers > 0 and total_connections > config.max_containers


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


def _pod_active_containers(
    containers: list[ContainerRecord],
    states: PodContainerStateReader | None,
) -> tuple[list[ContainerRecord], list[ContainerRecord]]:
    active = _active_containers(containers)
    if states is None:
        return active, []
    live: list[ContainerRecord] = []
    stale: list[ContainerRecord] = []
    for container in active:
        if container.status is ContainerStatus.Pending:
            live.append(container)
            continue
        state = states.get_container_state(container.id)
        if state is None or state.status not in {
            SchedulerContainerStatus.Pending,
            SchedulerContainerStatus.Running,
        }:
            stale.append(container)
            continue
        live.append(container)
    return live, stale


def _pending_container_count(containers: list[ContainerRecord]) -> int:
    return sum(1 for container in containers if container.status is ContainerStatus.Pending)


def _scheduler_status(
    redis: RedisClient,
    container_id: str,
) -> SchedulerContainerStatus | None:
    key = redis.key("scheduler", "containers", container_id, "state")
    raw = redis.hash_get_all(key).get("status")
    if raw is None:
        return None
    try:
        return SchedulerContainerStatus(_redis_text(raw))
    except ValueError:
        return None


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
    redis: RedisClient,
    stub: StubRecord,
    containers: list[ContainerRecord],
    *,
    keep_warm_seconds: int,
    now: datetime,
) -> list[ContainerRecord]:
    dispatch_records = dispatches.list_by_stub(stub.id)
    candidates = [
        container
        for container in containers
        if container.status is ContainerStatus.Running
        and _scheduler_status(redis, container.id) is not SchedulerContainerStatus.Stopping
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
    "AutoscaleAction",
    "AutoscaleResult",
    "AutoscalerIdentity",
    "AutoscalingDriver",
    "EndpointAutoscaler",
    "FunctionAutoscaler",
    "PodAutoscaler",
    "ScalePlan",
    "SchedulerServices",
    "WorkloadAutoscaler",
]
