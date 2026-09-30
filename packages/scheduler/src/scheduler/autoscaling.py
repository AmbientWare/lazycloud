from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import Protocol

from coordination.redis_client import RedisClient
from coordination.token_lock import release_token_lock, renew_token_lock
from database.records.apps import AutoscalingStubConfig, AutoscalingStubRecord, StubKind
from database.records.autoscaling import AutoscalingContainer
from database.repositories.apps import AppRepository, DeploymentRepository, StubRepository
from database.repositories.orchestration import AutoscalerStateRepository, ContainerRepository
from pydantic import Field, JsonValue
from shared.autoscaler_state import (
    SCALE_UP_FAILED_ACTION,
    AutoscaleAction,
    AutoscalerStateRecord,
    AutoscalerTargetKind,
    autoscaler_state_name,
)
from shared.autoscaling import (
    POD_WAKE_START_SECONDS,
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
from shared.containers import ContainerStatus
from shared.contracts import ContractModel
from shared.deployment_records import DEFAULT_DEVBOX_KEEP_WARM_SECONDS
from shared.env import truthy_env_value
from shared.errors import DomainError, EndpointReplicaLimitReachedError, InvalidInputError
from shared.http.endpoints import StartEndpointServeRequest, StartEndpointServeResponse
from shared.http.pods import CreatePodRequest, CreatePodResponse
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRequest,
)
from shared.timestamps import utc_now
from shared.worker_events import (
    ENDPOINT_SCALE_DECISION_ACTION,
    FUNCTION_SCALE_DECISION_ACTION,
    POD_SCALE_DECISION_ACTION,
)
from shared.workload_config import DEFAULT_FAILED_CONTAINER_WINDOW_SECONDS, StubConfig
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

type AutoscalingConfig = AutoscalingStubConfig | StubConfig

AUTOSCALER_LOCK_TTL_SECONDS = 10
AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD = 3
CONTAINER_DELIVERY_DEADLINE_SECONDS = 60
CONTAINER_START_DEADLINE_SECONDS = 600
"""Image preparation has a longer deadline than acknowledging a delivered request.

Both begin at assignment. The global backlog owns the separate wait for capacity.
"""
FUNCTION_AUTOSCALER_SOURCE = "function.autoscaler"
ENDPOINT_AUTOSCALER_SOURCE = "endpoint.autoscaler"
POD_AUTOSCALER_SOURCE = "pod.autoscaler"
_LIVE_CONTAINER_STATUSES = frozenset({ContainerStatus.Pending, ContainerStatus.Running})


class PodControl(Protocol):
    def create_pod(self, request: CreatePodRequest) -> CreatePodResponse: ...


class SchedulerContainerStateReader(Protocol):
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...

    def container_statuses(
        self,
        container_ids: Sequence[str],
    ) -> dict[str, SchedulerContainerStatus]: ...


class ContainerRequestReader(Protocol):
    def get_worker_request(
        self, worker_id: str, container_id: str
    ) -> SchedulerWorkerRequest | None: ...

    def has_recoverable_container_request(
        self,
        container_id: str,
        *,
        worker_id: str = "",
    ) -> bool: ...


class FunctionAutoscaleControl(Protocol):
    def start_function_containers(self, stub_id: str, *, desired_count: int) -> Iterator[str]: ...

    def task_demand_counts(self, stub_ids: Sequence[str]) -> dict[str, int]: ...

    def containers_holding_work(self, container_ids: Sequence[str]) -> set[str]: ...

    def fail_unclaimed_tasks(
        self,
        stub_id: str,
        *,
        error: str,
        limit: int = 100,
    ) -> int: ...


class EndpointAutoscaleControl(Protocol):
    def start_endpoint_containers(
        self,
        request: StartEndpointServeRequest,
        *,
        count: int,
    ) -> Iterator[StartEndpointServeResponse]: ...


@dataclass(frozen=True, slots=True)
class EndpointAutoscalingDispatchObservation:
    container_id: str | None
    active: bool
    finished_at: datetime | None


class EndpointAutoscalingDispatchReader(Protocol):
    def active_counts_by_stub(self, stub_ids: Sequence[str]) -> dict[str, int]: ...

    def observations_by_stub(
        self,
        stub_ids: Sequence[str],
        *,
        finished_since: datetime,
    ) -> dict[str, list[EndpointAutoscalingDispatchObservation]]: ...


@dataclass(frozen=True, slots=True)
class StaleContainer:
    """A record holding a ceiling slot, and the finding that says it should not."""

    record: AutoscalingContainer
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
    signal_name="task_demand",
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


@dataclass(frozen=True, slots=True)
class AutoscalingSignal:
    value: int
    endpoint_dispatches: tuple[EndpointAutoscalingDispatchObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class AutoscalingPlacementSnapshot:
    active_by_stub: Mapping[str, bool]
    containers_by_stub: Mapping[str, tuple[AutoscalingContainer, ...]]
    scheduler_statuses: Mapping[str, SchedulerContainerStatus]
    previous_states: Mapping[str, AutoscalerStateRecord]


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

    def selects(self, stub: AutoscalingStubRecord) -> bool: ...

    def samples(
        self, stubs: Sequence[AutoscalingStubRecord]
    ) -> Mapping[str, AutoscalingSignal]: ...

    def plan(
        self, stub: AutoscalingStubRecord, *, signal: int, current: int, now: datetime
    ) -> ScalePlan: ...

    def start(
        self, stub: AutoscalingStubRecord, *, current_count: int, desired_count: int
    ) -> Iterator[str]:
        """Yield committed starts, preserving partial success before a refusal."""
        ...

    def handle_failure_threshold(
        self,
        stub: AutoscalingStubRecord,
        failed_containers: Sequence[AutoscalingContainer],
    ) -> list[AutoscaleAction]: ...

    def scale_down(
        self,
        stub: AutoscalingStubRecord,
        containers: list[AutoscalingContainer],
        count: int,
        *,
        scheduler_statuses: Mapping[str, SchedulerContainerStatus],
        active_instance: bool,
        signal: AutoscalingSignal,
        now: datetime,
    ) -> list[AutoscaleAction]: ...


@dataclass(slots=True)
class AutoscalingDriver:
    """The reconcile pass every workload gets, identically.

    Stub selection and the pause an operator set, the stub's lock and contention
    metric, the container counts and which of them are real, the failed-container
    threshold, the inactive deployment, the workspace guardrail, and the metrics,
    event, and state row a lock-holding tick leaves behind: all of it here, once,
    for every kind, and none of it reachable from a workload.
    """

    services: SchedulerServices
    redis: RedisClient
    workload: WorkloadAutoscaler
    container_states: SchedulerContainerStateReader
    container_requests: ContainerRequestReader

    def selects(self, stub: AutoscalingStubRecord) -> bool:
        return self.workload.selects(stub) and _autoscaling_enabled(stub)

    def reconcile(
        self,
        stubs: Sequence[AutoscalingStubRecord] | None = None,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[AutoscaleResult]:
        current_time = now or utc_now()
        selected = (
            list(stubs)
            if stubs is not None
            else [
                stub
                for stub in self.services.control_plane_service.stubs.list_autoscaling_stubs()
                if self.selects(stub)
            ]
        )
        selected = selected[: max(limit, 0)]
        results: dict[str, AutoscaleResult] = {}
        with ExitStack() as locks:
            owned: list[AutoscalingStubRecord] = []
            tokens: dict[str, str] = {}
            for stub in selected:
                token = token_urlsafe(16)
                key = self._lock_key(stub)
                if not self.redis.set(key, token, nx=True, ex=AUTOSCALER_LOCK_TTL_SECONDS):
                    results[stub.id] = self._record_lock_contention(stub)
                    continue
                locks.callback(release_token_lock, self.redis, key, token)
                owned.append(stub)
                tokens[stub.id] = token
            if not owned:
                return list(results.values())
            # Read after acquiring ownership so an operator and a scheduled pass
            # cannot both decide from the state preceding the other's work.
            snapshot = load_autoscaling_placement_snapshot(
                self.services,
                self.container_states,
                owned,
                target_kind=self.workload.identity.kind,
                now=current_time,
            )
            signals = self.workload.samples(owned)

            def reconcile(stub: AutoscalingStubRecord) -> AutoscaleResult:
                if not renew_token_lock(
                    self.redis,
                    self._lock_key(stub),
                    tokens[stub.id],
                    ttl_seconds=AUTOSCALER_LOCK_TTL_SECONDS,
                ):
                    return self._record_lock_contention(
                        stub, reason="autoscaler ownership expired before execution"
                    )
                return self._reconcile_stub(
                    stub,
                    active=snapshot.active_by_stub.get(stub.id, False),
                    containers=list(snapshot.containers_by_stub.get(stub.id, ())),
                    scheduler_statuses=snapshot.scheduler_statuses,
                    signal=signals.get(stub.id, AutoscalingSignal(value=0)),
                    previous=snapshot.previous_states.get(stub.id),
                    now=current_time,
                )

            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="autoscaling") as executor:
                results.update(
                    (result.stub_id, result) for result in executor.map(reconcile, owned)
                )
        return [results[stub.id] for stub in selected]

    def _reconcile_stub(
        self,
        stub: AutoscalingStubRecord,
        *,
        active: bool,
        containers: list[AutoscalingContainer],
        scheduler_statuses: Mapping[str, SchedulerContainerStatus],
        signal: AutoscalingSignal,
        previous: AutoscalerStateRecord | None,
        now: datetime,
    ) -> AutoscaleResult:
        current_time = now
        identity = self.workload.identity
        holding, stale = _partition_backed_containers(
            containers,
            scheduler_statuses,
            self.container_requests,
            self.container_states,
            now=current_time,
        )
        actions, still_live = self._recover(stale)
        holding.extend(still_live)
        rollout_floor = max(
            (container.rollout_serving_floor or 0 for container in holding), default=0
        )
        holding = [container for container in holding if container.rollout_serving_floor is None]
        current = len(holding)
        pending = _pending_container_count(holding)
        plan = self.workload.plan(stub, signal=signal.value, current=current, now=current_time)
        # A container that fails on startup frees the slot it was counted in, so
        # the signal still reads as unserved and the next tick provisions again.
        # `max_containers` does not bound that — nothing is ever alive to count
        # against it — so a stub whose startup cannot succeed is started for as
        # long as the pressure sits there. This is what bounds it, and the
        # workspace guardrail below is what bounds the healthy case.
        recent_failed_containers = _recent_failed_containers(
            containers,
            now=current_time,
            window_seconds=_failed_container_window_seconds(stub.config),
            started_at=stub.power.woken_at,
        )
        failed_containers = [container.id for container in recent_failed_containers]
        failure_threshold = _failed_container_threshold(stub.config)
        failure_threshold_reached = (
            failure_threshold > 0 and len(failed_containers) >= failure_threshold
        )
        desired = max(plan.desired_containers, min(rollout_floor, plan.max_containers))
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
            actions.extend(self.workload.handle_failure_threshold(stub, recent_failed_containers))
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
            actions.extend(self._start(stub, current_count=current, desired_count=desired))
        elif delta < 0:
            actions.extend(
                self.workload.scale_down(
                    stub,
                    holding,
                    -delta,
                    scheduler_statuses=scheduler_statuses,
                    active_instance=active and not failure_threshold_reached,
                    signal=signal,
                    now=current_time,
                )
            )
        result = AutoscaleResult(
            kind=identity.kind,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            signal_name=identity.signal_name,
            signal_value=signal.value,
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
        self._record(stub, result, plan, previous)
        return result

    def _recover(
        self, stale: list[StaleContainer]
    ) -> tuple[list[AutoscaleAction], list[AutoscalingContainer]]:
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
        still_live: list[AutoscalingContainer] = []
        for container in stale:
            stopped = self.services.containers.stop(
                container.record.id,
                reason=StopContainerReason.Scheduler,
                only_if_pending=container.record.status is ContainerStatus.Pending,
            )
            if stopped.status in {ContainerStatus.Pending, ContainerStatus.Running}:
                still_live.append(
                    replace(
                        container.record,
                        status=stopped.status,
                        runtime_worker_id=stopped.runtime_worker_id,
                        started_at=stopped.started_at,
                    )
                )
                continue
            actions.append(
                AutoscaleAction(
                    container_id=stopped.id,
                    action="recover-stale",
                    reason=container.reason,
                )
            )
        return actions, still_live

    def _start(
        self, stub: AutoscalingStubRecord, *, current_count: int, desired_count: int
    ) -> list[AutoscaleAction]:
        actions: list[AutoscaleAction] = []
        try:
            for container_id in self.workload.start(
                stub, current_count=current_count, desired_count=desired_count
            ):
                actions.append(
                    AutoscaleAction(
                        container_id=container_id,
                        action="start",
                        reason="pressure requires more containers",
                    )
                )
        except DomainError as exc:
            actions.append(AutoscaleAction(action=SCALE_UP_FAILED_ACTION, reason=exc.message))
        return actions

    def _record(
        self,
        stub: AutoscalingStubRecord,
        result: AutoscaleResult,
        plan: ScalePlan,
        previous: AutoscalerStateRecord | None,
    ) -> None:
        identity = self.workload.identity
        state = AutoscalerStateRecord(
            name=autoscaler_state_name(identity.kind, stub.id),
            workspace_id=stub.workspace_id,
            target_id=stub.id,
            deployment_id=stub.deployment_id or "",
            app_id=stub.app_id or "",
            source=identity.source,
            target_kind=identity.kind,
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
            pending_count=result.pending_containers,
            guardrails=result.guardrails,
            last_actions=result.actions,
        )
        if (
            previous is None
            or result.actions
            or (previous.decision, previous.desired_count, previous.reason, previous.valid)
            != (state.decision, state.desired_count, state.reason, state.valid)
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
                "actions": [action.model_dump(mode="json") for action in result.actions],
            }
            self.services.events.emit(
                identity.scale_decision_action,
                resource_type="stub",
                resource_id=stub.id,
                message=f"{identity.kind.value} autoscaler selected desired container count",
                data=event_data,
                workspace_id=stub.workspace_id,
            )
        _record_autoscaler_metrics(self.services, stub, result, plan, source=identity.source)
        if _autoscaler_state_changed(previous, state):
            with self.services.context.database.session() as session:
                AutoscalerStateRepository(session).upsert(state)

    def _record_lock_contention(
        self, stub: AutoscalingStubRecord, *, reason: str = "autoscaler lock already held"
    ) -> AutoscaleResult:
        identity = self.workload.identity
        result = AutoscaleResult(
            kind=identity.kind,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            signal_name=identity.signal_name,
            decision=ScaleDecisionKind.Hold,
            reason=reason,
            lock_acquired=False,
        )
        self.services.metrics.increment(
            "autoscaler_lock_contentions_total",
            labels={
                "source": identity.source,
                "workspace_id": stub.workspace_id,
                "stub_id": stub.id,
                "kind": stub.kind.value,
            },
        )
        return result

    def _lock_key(self, stub: AutoscalingStubRecord) -> str:
        return self.redis.key(
            "autoscaling",
            self.workload.identity.lock_namespace,
            stub.workspace_id,
            stub.id,
            "lock",
        )


@dataclass(slots=True)
class FunctionAutoscaler:
    """Give a function's backlog enough containers to be worked through.

    The only thing that provisions a function past its first container. An
    invocation may bring an idle stub up so a cold call does not wait for a
    tick, and stops there; everything about depth is decided here, from the
    whole backlog, once per tick per stub.

    Demand counts claimed, unfinished tasks as well as the unclaimed ones, so a
    claim moves work between the two without shrinking the target and stopping
    the containers started for the rest of the backlog. Scale-down stops only
    containers holding no work: pending ones, and running ones whose stub never
    lets them retire on their own.
    """

    services: SchedulerServices
    functions: FunctionAutoscaleControl

    @property
    def identity(self) -> AutoscalerIdentity:
        return FUNCTION_AUTOSCALER

    def selects(self, stub: AutoscalingStubRecord) -> bool:
        # Every function stub, bound to a deployment or not. A stub reached by
        # `.remote()`, `.map()` or `lazycloud run` before anything is deployed
        # has a backlog like any other, and it is the one case where the first
        # container came from an invocation rather than from here — so refusing
        # it leaves a fan-out being served one container at a time.
        return stub.kind is StubKind.Function

    def samples(self, stubs: Sequence[AutoscalingStubRecord]) -> Mapping[str, AutoscalingSignal]:
        counts = self.functions.task_demand_counts([stub.id for stub in stubs])
        return {stub.id: AutoscalingSignal(value=counts.get(stub.id, 0)) for stub in stubs}

    def plan(
        self, stub: AutoscalingStubRecord, *, signal: int, current: int, now: datetime
    ) -> ScalePlan:
        del now
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

    def start(
        self, stub: AutoscalingStubRecord, *, current_count: int, desired_count: int
    ) -> Iterator[str]:
        return self.functions.start_function_containers(stub.id, desired_count=desired_count)

    def handle_failure_threshold(
        self,
        stub: AutoscalingStubRecord,
        failed_containers: Sequence[AutoscalingContainer],
    ) -> list[AutoscaleAction]:
        error = next(
            (container.startup_error for container in failed_containers if container.startup_error),
            "function containers failed to start",
        )
        failed_count = self.functions.fail_unclaimed_tasks(stub.id, error=error)
        if failed_count == 0:
            return []
        return [
            AutoscaleAction(
                action="fail-unclaimed-tasks",
                reason=f"failed {failed_count} queued tasks: {error}",
            )
        ]

    def scale_down(
        self,
        stub: AutoscalingStubRecord,
        containers: list[AutoscalingContainer],
        count: int,
        *,
        scheduler_statuses: Mapping[str, SchedulerContainerStatus],
        active_instance: bool,
        signal: AutoscalingSignal,
        now: datetime,
    ) -> list[AutoscaleAction]:
        """Release unassigned excess capacity and idle containers without an expiry.

        Assigned startups finish preparation and then observe their idle window.
        Unassigned containers have no idle window and may wait for capacity forever.
        Inactive workloads release assigned startups too.
        """

        del scheduler_statuses, signal, now
        pending_only = stub.config.runtime.keep_warm >= 0
        preserve_assigned_startup = active_instance and pending_only
        busy = self.functions.containers_holding_work([item.id for item in containers])
        idle = [
            container
            for container in containers
            if container.id not in busy
            and (not pending_only or container.status is ContainerStatus.Pending)
            and (not preserve_assigned_startup or not container.runtime_worker_id)
            and not truthy_env_value(container.hot_reload)
        ]
        idle.sort(key=lambda container: container.created_at, reverse=True)
        actions: list[AutoscaleAction] = []
        for container in idle[:count]:
            stopped = self.services.containers.stop(
                container.id,
                reason=StopContainerReason.Scheduler,
                only_if_pending=pending_only,
                only_if_unassigned=preserve_assigned_startup,
            )
            if stopped.status in {ContainerStatus.Pending, ContainerStatus.Running}:
                continue
            actions.append(
                AutoscaleAction(
                    container_id=stopped.id,
                    action="stop",
                    reason="container count exceeds workload demand and warm floor",
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

    def selects(self, stub: AutoscalingStubRecord) -> bool:
        return stub.kind in {StubKind.Endpoint, StubKind.Asgi} and bool(stub.deployment_id)

    def samples(self, stubs: Sequence[AutoscalingStubRecord]) -> Mapping[str, AutoscalingSignal]:
        stub_ids = [stub.id for stub in stubs]
        current_time = utc_now()
        keep_warm_seconds = max(
            (_endpoint_autoscaler_config(stub.config).keep_warm_seconds for stub in stubs),
            default=0,
        )
        counts = self.dispatches.active_counts_by_stub(stub_ids)
        observations = self.dispatches.observations_by_stub(
            stub_ids,
            finished_since=current_time - timedelta(seconds=max(keep_warm_seconds, 0)),
        )
        return {
            stub.id: AutoscalingSignal(
                value=counts.get(stub.id, 0),
                endpoint_dispatches=tuple(observations.get(stub.id, ())),
            )
            for stub in stubs
        }

    def plan(
        self, stub: AutoscalingStubRecord, *, signal: int, current: int, now: datetime
    ) -> ScalePlan:
        del now
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

    def start(
        self, stub: AutoscalingStubRecord, *, current_count: int, desired_count: int
    ) -> Iterator[str]:
        try:
            for response in self.endpoints.start_endpoint_containers(
                StartEndpointServeRequest(stub_id=stub.id), count=desired_count - current_count
            ):
                yield response.container_id
        except EndpointReplicaLimitReachedError:
            return

    def handle_failure_threshold(
        self,
        stub: AutoscalingStubRecord,
        failed_containers: Sequence[AutoscalingContainer],
    ) -> list[AutoscaleAction]:
        del stub, failed_containers
        return []

    def scale_down(
        self,
        stub: AutoscalingStubRecord,
        containers: list[AutoscalingContainer],
        count: int,
        *,
        scheduler_statuses: Mapping[str, SchedulerContainerStatus],
        active_instance: bool,
        signal: AutoscalingSignal,
        now: datetime,
    ) -> list[AutoscaleAction]:
        del active_instance
        actions: list[AutoscaleAction] = []
        for container in _stoppable_endpoint_containers(
            list(signal.endpoint_dispatches),
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

    def selects(self, stub: AutoscalingStubRecord) -> bool:
        return stub.kind in {StubKind.Pod, StubKind.Sandbox} and (
            stub.kind is StubKind.Sandbox or bool(stub.deployment_id)
        )

    def samples(self, stubs: Sequence[AutoscalingStubRecord]) -> Mapping[str, AutoscalingSignal]:
        if not stubs:
            return {}
        values = self.redis.mget(
            [
                self.redis.key(pod_total_connections_key(stub.workspace_id, stub.id))
                for stub in stubs
            ]
        )
        return {
            stub.id: AutoscalingSignal(value=_redis_non_negative_int(value))
            for stub, value in zip(stubs, values, strict=True)
        }

    def plan(
        self, stub: AutoscalingStubRecord, *, signal: int, current: int, now: datetime
    ) -> ScalePlan:
        config = _pod_autoscaler_config(stub, now=now)
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

    def start(
        self, stub: AutoscalingStubRecord, *, current_count: int, desired_count: int
    ) -> Iterator[str]:
        for _ in range(desired_count - current_count):
            response = self.pods.create_pod(CreatePodRequest(stub_id=stub.id))
            if not response.container_id:
                raise InvalidInputError("pod create returned no container id")
            yield response.container_id

    def handle_failure_threshold(
        self,
        stub: AutoscalingStubRecord,
        failed_containers: Sequence[AutoscalingContainer],
    ) -> list[AutoscaleAction]:
        """End the start that asked for these containers.

        A pending start keeps the pod wanting a container whoever is connected,
        and a devbox reports it as queued rather than as the startup failure.
        A start made since this pass read the stub is kept.
        """
        del failed_containers
        woken_at = stub.power.woken_at
        if woken_at is not None:
            with self.services.context.database.session() as session:
                StubRepository(session).end_start(
                    stub.id, workspace_id=stub.workspace_id, woken_at=woken_at
                )
        return []

    def scale_down(
        self,
        stub: AutoscalingStubRecord,
        containers: list[AutoscalingContainer],
        count: int,
        *,
        scheduler_statuses: Mapping[str, SchedulerContainerStatus],
        active_instance: bool,
        signal: AutoscalingSignal,
        now: datetime,
    ) -> list[AutoscaleAction]:
        del scheduler_statuses, signal
        workspace_id = stub.workspace_id
        # A fixed replica count is an explicit scale target. Its excess replicas
        # drain connections without retaining an additional idle window.
        autoscaler = stub.config.autoscaler
        keep_warm_seconds = (
            0
            if stub.kind is StubKind.Sandbox
            or autoscaler.min_containers == autoscaler.max_containers
            else _pod_autoscaler_config(stub, now=now).keep_warm_seconds
        )
        woken_at = stub.power.woken_at
        states = _pod_container_states(self.redis, workspace_id, stub, containers)
        stop_plan = select_stoppable_pod_containers(
            states,
            # A parked pod keeps nothing warm: its owner asked for it to stop.
            active_instance=active_instance and not stub.power.parked,
            keep_warm_seconds=keep_warm_seconds,
            keep_warm_lock_authoritative=stub.kind is StubKind.Sandbox,
            now_seconds=int(now.timestamp()),
            woken_at_seconds=int(woken_at.timestamp()) if woken_at is not None else None,
            wake_window_seconds=max(
                stub.config.runtime.keep_warm, DEFAULT_DEVBOX_KEEP_WARM_SECONDS
            ),
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


def _autoscaling_enabled(stub: AutoscalingStubRecord) -> bool:
    raw = stub.config.metadata.get("autoscaling_enabled", True)
    return raw is not False


def _function_autoscaler_config(config: AutoscalingConfig) -> BacklogAutoscalerConfig:
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
    stub: AutoscalingStubRecord,
    result: AutoscaleResult,
    plan: ScalePlan,
    *,
    source: str,
) -> None:
    labels = {
        "source": source,
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": stub.kind.value,
    }
    services.metrics.increment(
        "autoscaler_decisions_total",
        labels={**labels, "decision": result.decision.value, "reason": result.reason},
    )
    throttled = result.guardrails.get("limited") is True
    for name, value in {
        "current_containers": result.current_containers,
        "desired_containers": result.desired_containers,
        "pending_containers": result.pending_containers,
        "idle_containers": max(result.current_containers - result.desired_containers, 0),
        "failed_containers": len(result.failed_containers),
        "failed_container_threshold_reached": int(
            result.reason == "failed container threshold reached"
        ),
        "max_containers": plan.max_containers,
        "guardrail_throttled": int(throttled),
    }.items():
        services.metrics.set_gauge(f"autoscaler_{name}", value, labels=labels)
    signal_labels = {**labels, "signal": result.signal_name}
    services.metrics.set_gauge("autoscaler_signal", result.signal_value, labels=signal_labels)
    services.metrics.set_gauge(
        "autoscaler_pressure_saturated",
        int(_pressure_saturated(result.signal_value, plan)),
        labels=signal_labels,
    )
    if throttled:
        reason = result.guardrails.get("reason")
        services.metrics.increment(
            "autoscaler_throttled_total",
            labels={
                **labels,
                "reason": reason if isinstance(reason, str) and reason else "guardrail-limited",
            },
        )
    no_worker_capacity = False
    for action in result.actions:
        if not action.action:
            continue
        action_labels = {**labels, "action": action.action}
        services.metrics.increment("autoscaler_actions_total", labels=action_labels)
        if "failed" in action.action:
            services.metrics.increment("autoscaler_scale_failures_total", labels=action_labels)
            if "worker capacity" in action.reason.lower():
                no_worker_capacity = True
                services.metrics.increment("autoscaler_no_worker_capacity_total", labels=labels)
    services.metrics.set_gauge(
        "autoscaler_no_worker_capacity",
        int(no_worker_capacity),
        labels=labels,
    )


def _autoscaler_state_changed(
    previous: AutoscalerStateRecord | None,
    current: AutoscalerStateRecord,
) -> bool:
    if previous is None:
        return True
    return previous.model_dump(exclude={"updated_at"}) != current.model_dump(exclude={"updated_at"})


def load_autoscaling_placement_snapshot(
    services: SchedulerServices,
    container_states: SchedulerContainerStateReader,
    stubs: Sequence[AutoscalingStubRecord],
    *,
    target_kind: AutoscalerTargetKind,
    now: datetime | None = None,
) -> AutoscalingPlacementSnapshot:
    selected = tuple(stubs)
    if not selected:
        return AutoscalingPlacementSnapshot(
            active_by_stub={},
            containers_by_stub={},
            scheduler_statuses={},
            previous_states={},
        )
    current_time = now or utc_now()
    failed_window_seconds = max(
        (_failed_container_window_seconds(stub.config) for stub in selected),
        default=DEFAULT_FAILED_CONTAINER_WINDOW_SECONDS,
    )
    stub_ids = [stub.id for stub in selected]
    app_ids = [stub.app_id for stub in selected if stub.app_id]
    deployment_ids = [stub.deployment_id for stub in selected if stub.deployment_id]
    with services.context.database.session() as session:
        containers = ContainerRepository(session).autoscaling_candidates(
            stub_ids=stub_ids,
            failed_since=current_time - timedelta(seconds=max(failed_window_seconds, 0)),
        )
        active_apps = AppRepository(session).active_by_ids(app_ids)
        active_deployments = DeploymentRepository(session).active_by_ids(deployment_ids)
        previous_states = AutoscalerStateRepository(session).get_many(
            [(stub.workspace_id, target_kind, stub.id) for stub in selected]
        )
    grouped: dict[str, list[AutoscalingContainer]] = {stub_id: [] for stub_id in stub_ids}
    for container in containers:
        if container.stub_id in grouped:
            grouped[container.stub_id].append(container)
    active_by_stub = {
        stub.id: (not stub.app_id or active_apps.get(stub.app_id, False))
        and (not stub.deployment_id or active_deployments.get(stub.deployment_id, False))
        for stub in selected
    }
    scheduler_statuses = container_states.container_statuses(
        [container.id for container in containers if container.status in _LIVE_CONTAINER_STATUSES]
    )
    return AutoscalingPlacementSnapshot(
        active_by_stub=active_by_stub,
        containers_by_stub={key: tuple(value) for key, value in grouped.items()},
        scheduler_statuses=scheduler_statuses,
        previous_states={
            stub.id: previous_states[(stub.workspace_id, target_kind, stub.id)]
            for stub in selected
            if (stub.workspace_id, target_kind, stub.id) in previous_states
        },
    )


def _active_containers(containers: list[AutoscalingContainer]) -> list[AutoscalingContainer]:
    return [container for container in containers if container.status in _LIVE_CONTAINER_STATUSES]


def _partition_backed_containers(
    containers: list[AutoscalingContainer],
    scheduler_statuses: Mapping[str, SchedulerContainerStatus],
    requests: ContainerRequestReader,
    states: SchedulerContainerStateReader,
    *,
    now: datetime,
) -> tuple[list[AutoscalingContainer], list[StaleContainer]]:
    """Split records the scheduler still backs from records that only look live.

    The durable row is what the ceiling is counted from, so a row that says
    `pending` or `running` while nothing is going to make it true is a slot held
    against a workload that cannot use it. At `max_containers = 1` that is not a
    degradation, it is a stop: desired equals current forever and the backlog
    grows one entry per fire.
    """

    live: list[AutoscalingContainer] = []
    stale: list[StaleContainer] = []
    for container in _active_containers(containers):
        reason = _stale_reason(
            container,
            scheduler_statuses.get(container.id),
            requests,
            states,
            now=now,
        )
        if reason:
            stale.append(StaleContainer(record=container, reason=reason))
            continue
        live.append(container)
    return live, stale


def _stale_reason(
    container: AutoscalingContainer,
    scheduler_status: SchedulerContainerStatus | None,
    requests: ContainerRequestReader,
    states: SchedulerContainerStateReader,
    *,
    now: datetime,
) -> str:
    """Why this record is not capacity, or empty where it still is.

    Read from the durable row and from what actually holds the container, never
    from whether a Redis key happens to be alive: the scheduler state is
    re-armed by whoever holds it, so a worker wedged half-way through a start
    refreshes it indefinitely, and a durable row that only becomes true when a
    cache entry expires has the ownership backwards.

    Running containers remain capacity during cache recovery. The orphan
    reconciler confirms loss of the container and its worker before stopping it.

    The global backlog owns capacity waits. A worker delivery has a shorter
    deadline until acknowledged; container startup gets its full deadline after
    assignment, including when capacity acquisition took longer than startup.
    """

    if scheduler_status is not None and scheduler_status not in {
        SchedulerContainerStatus.Pending,
        SchedulerContainerStatus.Running,
    }:
        return f"scheduler state is {scheduler_status.value}"
    if container.status is ContainerStatus.Running:
        return ""
    if scheduler_status is SchedulerContainerStatus.Running:
        # Started, and only the durable row has yet to catch up.
        return ""
    if requests.has_recoverable_container_request(container.id):
        return ""
    if container.runtime_worker_id:
        delivery = requests.get_worker_request(container.runtime_worker_id, container.id)
        if delivery is not None:
            if (now - delivery.timestamp).total_seconds() < CONTAINER_DELIVERY_DEADLINE_SECONDS:
                return ""
            return "worker did not acknowledge the container within the delivery deadline"
    state = states.get_container_state(container.id)
    startup_at = (
        state.scheduled_at if state is not None and state.worker_id else container.created_at
    )
    if (now - startup_at).total_seconds() < CONTAINER_START_DEADLINE_SECONDS:
        return ""
    return "container never started within the start deadline"


def _pending_container_count(containers: list[AutoscalingContainer]) -> int:
    return sum(1 for container in containers if container.status is ContainerStatus.Pending)


def _recent_failed_containers(
    containers: list[AutoscalingContainer],
    *,
    now: datetime,
    window_seconds: int,
    started_at: datetime | None,
) -> list[AutoscalingContainer]:
    cutoff = now - timedelta(seconds=max(window_seconds, 0))
    # An explicit start asks for a fresh attempt, so failures before it do not count.
    if started_at is not None:
        cutoff = max(cutoff, started_at)
    failed: list[AutoscalingContainer] = []
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
    return failed


def _stoppable_endpoint_containers(
    dispatch_records: list[EndpointAutoscalingDispatchObservation],
    containers: list[AutoscalingContainer],
    scheduler_statuses: Mapping[str, SchedulerContainerStatus],
    *,
    keep_warm_seconds: int,
    now: datetime,
) -> list[AutoscalingContainer]:
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
    container: AutoscalingContainer,
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


def _pod_container_states(
    redis: RedisClient,
    workspace_id: str,
    stub: AutoscalingStubRecord,
    containers: list[AutoscalingContainer],
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


def _endpoint_autoscaler_config(config: AutoscalingConfig) -> EndpointAutoscalerConfig:
    autoscaler = config.autoscaler
    runtime_config = config.runtime
    return EndpointAutoscalerConfig(
        min_containers=autoscaler.min_containers,
        max_containers=autoscaler.max_containers,
        tasks_per_container=autoscaler.tasks_per_container,
        keep_warm_seconds=max(runtime_config.keep_warm, 0),
    )


def _pod_autoscaler_config(stub: AutoscalingStubRecord, *, now: datetime) -> PodAutoscalerConfig:
    autoscaler = stub.config.autoscaler
    runtime_config = stub.config.runtime
    keep_warm_seconds = max(runtime_config.keep_warm, -1)
    min_containers = autoscaler.min_containers
    if keep_warm_seconds == -1:
        min_containers = max(min_containers, 1)
    woken_at = stub.power.woken_at
    return PodAutoscalerConfig(
        stub_type=(
            PodStubType.Sandbox if stub.kind is StubKind.Sandbox else PodStubType.PodDeployment
        ),
        min_containers=min_containers,
        max_containers=autoscaler.max_containers,
        keep_warm_seconds=keep_warm_seconds,
        parked=stub.power.parked,
        woken=woken_at is not None and now < woken_at + timedelta(seconds=POD_WAKE_START_SECONDS),
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


def _failed_container_threshold(config: AutoscalingConfig) -> int:
    return _first_configured_non_negative(
        config.autoscaler.failed_container_threshold,
        config.autoscaler.max_failed_containers,
        config.autoscaler.failure_threshold,
        default=AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD,
    )


def _failed_container_window_seconds(config: AutoscalingConfig) -> int:
    return config.autoscaler.failed_container_window


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
