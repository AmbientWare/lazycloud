from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
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
    PodAutoscalerConfig,
    PodAutoscalerSample,
    PodContainerState,
    PodScaleDecisionKind,
    PodStubType,
    TaskQueueAutoscalerConfig,
    TaskQueueAutoscalerSample,
    TaskQueueScaleDecision,
    TaskQueueScaleDecisionKind,
    decide_pod_scale,
    decide_task_queue_scale,
    select_stoppable_pod_containers,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.errors import DomainError, NotFoundError
from shared.http.endpoints import StartEndpointServeRequest, StartEndpointServeResponse
from shared.http.pods import CreatePodRequest, CreatePodResponse
from shared.http.taskqueues import StartTaskQueueServeRequest, StartTaskQueueServeResponse
from shared.scheduling import SchedulerContainerState, SchedulerContainerStatus
from shared.timestamps import utc_now
from shared.worker_events import (
    ENDPOINT_SCALE_DECISION_ACTION,
    POD_SCALE_DECISION_ACTION,
    TASK_QUEUE_SCALE_DECISION_ACTION,
)
from shared.workload_config import StubConfig
from shared.workload_keys import (
    pod_container_connections_key,
    pod_keep_warm_lock_key,
    pod_total_connections_key,
    task_queue_keep_warm_lock_key,
    task_queue_processing_lock_key,
    task_queue_running_lock_index_key,
    task_queue_running_lock_key,
)

from scheduler.autoscaling_guardrails import (
    AutoscalerGuardrailPlan,
    plan_autoscaler_start_guardrails,
)
from scheduler.services import (
    SchedulerServices,
)

TASK_QUEUE_AUTOSCALER_LOCK_TTL_SECONDS = 10
TASK_QUEUE_AUTOSCALER_DEFAULT_TIMEOUT_SECONDS = 600
TASK_QUEUE_AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD = 3
TASK_QUEUE_AUTOSCALER_DEFAULT_FAILURE_WINDOW_SECONDS = 300
FUNCTION_AUTOSCALER_SOURCE = "function.autoscaler"
TASK_QUEUE_AUTOSCALER_SOURCE = "taskqueue.autoscaler"
ENDPOINT_AUTOSCALER_LOCK_TTL_SECONDS = 10
ENDPOINT_AUTOSCALER_DEFAULT_TIMEOUT_SECONDS = 600
ENDPOINT_AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD = 3
ENDPOINT_AUTOSCALER_DEFAULT_FAILURE_WINDOW_SECONDS = 300
ENDPOINT_AUTOSCALER_SOURCE = "endpoint.autoscaler"
POD_AUTOSCALER_LOCK_TTL_SECONDS = 10
POD_AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD = 3
POD_AUTOSCALER_DEFAULT_FAILURE_WINDOW_SECONDS = 300
POD_AUTOSCALER_SOURCE = "pod.autoscaler"


class PodControl(Protocol):
    def create_pod(self, request: CreatePodRequest) -> CreatePodResponse: ...

    def expire_pods(self, *, now: datetime | None = None) -> list[ContainerRecord]: ...


class PodContainerStateReader(Protocol):
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...


class TaskQueueAutoscaleControl(Protocol):
    def expire_pending_tasks(self, stub_id: str, *, now: datetime | None = None) -> int: ...

    def start_task_queue_serve(
        self,
        request: StartTaskQueueServeRequest,
    ) -> StartTaskQueueServeResponse: ...


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


class TaskQueueAutoscaleAction(ContractModel):
    container_id: str
    action: str
    reason: str = ""


class TaskQueueAutoscaleResult(ContractModel):
    stub_id: str
    workspace_id: str
    queue_length: int = 0
    running_tasks: int = 0
    current_containers: int = 0
    pending_containers: int = 0
    desired_containers: int = 0
    decision: TaskQueueScaleDecisionKind
    reason: str
    active: bool = True
    lock_acquired: bool = True
    valid: bool = True
    failed_containers: list[str] = Field(default_factory=list)
    actions: list[TaskQueueAutoscaleAction] = Field(default_factory=list)
    guardrails: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.actions)


class EndpointScaleDecisionKind(StrEnum):
    ScaleUp = "scale-up"
    ScaleDown = "scale-down"
    Hold = "hold"
    Invalid = "invalid"


class EndpointAutoscaleAction(ContractModel):
    container_id: str
    action: str
    reason: str = ""


class EndpointAutoscaleResult(ContractModel):
    stub_id: str
    workspace_id: str
    active_requests: int = 0
    current_containers: int = 0
    pending_containers: int = 0
    desired_containers: int = 0
    decision: EndpointScaleDecisionKind
    reason: str
    active: bool = True
    lock_acquired: bool = True
    valid: bool = True
    failed_containers: list[str] = Field(default_factory=list)
    actions: list[EndpointAutoscaleAction] = Field(default_factory=list)
    guardrails: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.actions)


class PodAutoscaleAction(ContractModel):
    container_id: str
    action: str
    reason: str = ""


class PodAutoscaleResult(ContractModel):
    stub_id: str
    workspace_id: str
    total_connections: int = 0
    current_containers: int = 0
    pending_containers: int = 0
    desired_containers: int = 0
    decision: PodScaleDecisionKind
    reason: str
    active: bool = True
    lock_acquired: bool = True
    valid: bool = True
    failed_containers: list[str] = Field(default_factory=list)
    actions: list[PodAutoscaleAction] = Field(default_factory=list)
    guardrails: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.actions)


type AutoscaleAction = TaskQueueAutoscaleAction | EndpointAutoscaleAction | PodAutoscaleAction


class FunctionAutoscaleControl(Protocol):
    def start_function_container(self, stub_id: str) -> bool: ...

    def unclaimed_task_count(self, stub_id: str) -> int: ...


class FunctionAutoscaleAction(ContractModel):
    container_id: str
    action: str
    reason: str = ""


class FunctionAutoscaleResult(ContractModel):
    stub_id: str
    workspace_id: str
    queue_length: int = 0
    current_containers: int = 0
    pending_containers: int = 0
    desired_containers: int = 0
    decision: TaskQueueScaleDecisionKind
    reason: str
    active: bool = True
    lock_acquired: bool = True
    valid: bool = True
    actions: list[FunctionAutoscaleAction] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.actions)


@dataclass(slots=True)
class FunctionAutoscalingService:
    """Give a function's backlog enough containers to be worked through.

    Written as a fourth service rather than folded into a shared base. Three of
    the four are about to become three of three when task queues go, and an
    abstraction extracted now would be shaped around the member that is leaving.

    The scaling rule itself is reused rather than rewritten: a function's
    backlog and a task queue's are the same sample — how much is waiting, over
    how much one container takes, capped by what the stub allows. Two rules
    would disagree eventually, and the disagreement would show up as a bill.

    Scaling down is deliberately not done here. A function container already
    ends itself when its keep-warm window passes with no work, so the way to
    have fewer is to stop giving them any — stopping one from outside risks
    taking an invocation with it, which is the one failure this whole change
    has been careful about.
    """

    services: SchedulerServices
    redis: RedisClient
    functions: FunctionAutoscaleControl

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[FunctionAutoscaleResult]:
        current_time = now or utc_now()
        stubs = [
            stub
            for stub in self.services.scheduler_workloads.list_stubs()
            if stub.kind is StubKind.Function
        ]
        results: list[FunctionAutoscaleResult] = []
        for stub in stubs[: max(limit, 0)]:
            token = token_urlsafe(16)
            lock_key = self._lock_key(stub)
            if not self._acquire_lock(lock_key, token):
                results.append(
                    FunctionAutoscaleResult(
                        stub_id=stub.id,
                        workspace_id=stub.workspace_id,
                        decision=TaskQueueScaleDecisionKind.Hold,
                        reason="autoscaler lock already held",
                        lock_acquired=False,
                    )
                )
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
    ) -> FunctionAutoscaleResult:
        del now
        active = _deployment_active(self.services, stub)
        containers = _containers_for_stub(self.services, stub)
        current = len(_active_containers(containers))
        pending = _pending_container_count(containers)
        queue_length = self.functions.unclaimed_task_count(stub.id)
        config = _function_autoscaler_config(stub.config)
        sample = TaskQueueAutoscalerSample(
            queue_length=queue_length,
            running_tasks=0,
            current_containers=current,
        )
        decision = decide_task_queue_scale(sample, config)
        desired = decision.desired_containers if active else 0
        reason = "deployment inactive" if not active else decision.reason.value
        actions: list[FunctionAutoscaleAction] = []
        if active and decision.valid and desired > current:
            actions.extend(self._scale_up(stub, desired - current))
        result = FunctionAutoscaleResult(
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            queue_length=queue_length,
            current_containers=current,
            pending_containers=pending,
            desired_containers=desired,
            decision=decision.decision,
            reason=reason,
            active=active,
            valid=decision.valid,
            actions=actions,
        )
        _record_autoscaler_state(
            self.services,
            source=FUNCTION_AUTOSCALER_SOURCE,
            target_kind=AutoscalerTargetKind.Function,
            stub=stub,
            current_count=current,
            desired_count=desired,
            decision=result.decision.value,
            reason=reason,
            active=active,
            valid=result.valid,
            signal_name="unclaimed_tasks",
            signal_value=queue_length,
        )
        return result

    def _scale_up(self, stub: StubRecord, count: int) -> list[FunctionAutoscaleAction]:
        actions: list[FunctionAutoscaleAction] = []
        for _ in range(count):
            try:
                started = self.functions.start_function_container(stub.id)
            except DomainError as exc:
                actions.append(
                    FunctionAutoscaleAction(
                        container_id="",
                        action="scale-up-failed",
                        reason=exc.message,
                    )
                )
                break
            if not started:
                break
            actions.append(FunctionAutoscaleAction(container_id="", action="scale-up"))
        return actions

    def _lock_key(self, stub: StubRecord) -> str:
        return self.redis.key("autoscaling", "functions", stub.workspace_id, stub.id, "lock")

    def _acquire_lock(self, key: str, token: str) -> bool:
        return bool(
            self.redis.set(
                key,
                token,
                nx=True,
                ex=TASK_QUEUE_AUTOSCALER_LOCK_TTL_SECONDS,
            )
        )

    def _release_lock(self, key: str, token: str) -> None:
        if _redis_text(self.redis.get(key)) == token:
            self.redis.delete(key)


def _function_autoscaler_config(config: StubConfig) -> TaskQueueAutoscalerConfig:
    autoscaler = config.autoscaler
    return TaskQueueAutoscalerConfig(
        tasks_per_container=autoscaler.tasks_per_container,
        # A function with no autoscaler configured still has to be able to run,
        # so an unset ceiling means one container rather than none.
        max_containers=max(autoscaler.max_containers, 1),
    )


@dataclass(slots=True)
class TaskQueueAutoscalingService:
    services: SchedulerServices
    redis: RedisClient
    task_queues: TaskQueueAutoscaleControl

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[TaskQueueAutoscaleResult]:
        current_time = now or utc_now()
        stubs = [
            stub
            for stub in self.services.scheduler_workloads.list_stubs()
            if stub.kind is StubKind.TaskQueue and _task_queue_autoscaling_enabled(stub)
        ]
        results: list[TaskQueueAutoscaleResult] = []
        for stub in stubs[: max(limit, 0)]:
            token = token_urlsafe(16)
            lock_key = self._lock_key(stub)
            if not self._acquire_lock(lock_key, token):
                result = TaskQueueAutoscaleResult(
                    stub_id=stub.id,
                    workspace_id=stub.workspace_id,
                    decision=TaskQueueScaleDecisionKind.Hold,
                    reason="autoscaler lock already held",
                    lock_acquired=False,
                )
                _record_autoscaler_state(
                    self.services,
                    source=TASK_QUEUE_AUTOSCALER_SOURCE,
                    target_kind=AutoscalerTargetKind.TaskQueue,
                    stub=stub,
                    current_count=result.current_containers,
                    desired_count=result.desired_containers,
                    decision=result.decision.value,
                    reason=result.reason,
                    active=result.active,
                    valid=result.valid,
                    lock_acquired=result.lock_acquired,
                    owner_lock_key=lock_key,
                )
                results.append(result)
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
    ) -> TaskQueueAutoscaleResult:
        current_time = now or utc_now()
        self.task_queues.expire_pending_tasks(stub.id, now=current_time)
        active = _deployment_active(self.services, stub)
        containers = _task_queue_containers(self.services, stub)
        current = len(_active_containers(containers))
        pending = _pending_container_count(containers)
        queue_length = _queue_length(self.services, stub)
        workspace_id = stub.workspace_id
        running_tasks = _running_task_count(self.redis, workspace_id, stub, containers)
        config = _task_queue_autoscaler_config(stub.config)
        failed_containers = _recent_failed_task_queue_container_ids(
            containers,
            now=current_time,
            window_seconds=_failed_container_window_seconds(stub.config),
        )
        failure_threshold = _failed_container_threshold(stub.config)
        failure_threshold_reached = (
            failure_threshold > 0 and len(failed_containers) >= failure_threshold
        )
        sample = TaskQueueAutoscalerSample(
            queue_length=queue_length,
            running_tasks=running_tasks,
            current_containers=current,
        )
        decision = decide_task_queue_scale(sample, config)
        desired = decision.desired_containers if active else 0
        reason = "deployment inactive" if not active else decision.reason.value
        result_decision = decision.decision
        if active and failure_threshold_reached:
            desired = 0
            reason = "failed container threshold reached"
            result_decision = (
                TaskQueueScaleDecisionKind.ScaleDown
                if current > 0
                else TaskQueueScaleDecisionKind.Hold
            )
        guardrail = AutoscalerGuardrailPlan()
        if active and decision.valid and desired > current:
            guardrail = plan_autoscaler_start_guardrails(
                self.redis,
                stub=stub,
                current_count=current,
                desired_count=desired,
            )
            if guardrail.limited:
                desired = guardrail.desired_count
                reason = guardrail.reason
                result_decision = _task_queue_scale_kind(desired, current)
        actions: list[TaskQueueAutoscaleAction] = []
        if decision.valid:
            delta = desired - current
            if delta > 0:
                actions.extend(self._scale_up(stub, delta))
            elif delta < 0:
                actions.extend(self._scale_down(stub, workspace_id, containers, -delta))
        result = TaskQueueAutoscaleResult(
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            queue_length=queue_length,
            running_tasks=running_tasks,
            current_containers=current,
            pending_containers=pending,
            desired_containers=desired,
            decision=result_decision,
            reason=reason,
            active=active,
            valid=decision.valid,
            failed_containers=failed_containers,
            actions=actions,
            guardrails=guardrail.payload(),
        )
        self._emit_scale_event(stub, result, config, decision)
        return result

    def _scale_up(self, stub: StubRecord, count: int) -> list[TaskQueueAutoscaleAction]:
        actions: list[TaskQueueAutoscaleAction] = []
        timeout = _task_queue_timeout_seconds(stub.config)
        for _ in range(count):
            try:
                response = self.task_queues.start_task_queue_serve(
                    StartTaskQueueServeRequest(stub_id=stub.id, timeout=timeout)
                )
            except DomainError as exc:
                actions.append(
                    TaskQueueAutoscaleAction(
                        container_id="",
                        action="scale-up-failed",
                        reason=exc.message,
                    )
                )
                break
            actions.append(
                TaskQueueAutoscaleAction(
                    container_id=response.container_id,
                    action="start",
                    reason="queue depth requires more consumers",
                )
            )
        return actions

    def _scale_down(
        self,
        stub: StubRecord,
        workspace_id: str,
        containers: list[ContainerRecord],
        count: int,
    ) -> list[TaskQueueAutoscaleAction]:
        actions: list[TaskQueueAutoscaleAction] = []
        for container in _stoppable_task_queue_containers(
            self.redis,
            workspace_id,
            stub,
            containers,
        ):
            if len(actions) >= count:
                break
            stopped = self.services.containers.stop(container.id)
            actions.append(
                TaskQueueAutoscaleAction(
                    container_id=stopped.id,
                    action="stop",
                    reason="queue depth no longer requires consumer",
                )
            )
        return actions

    def _emit_scale_event(
        self,
        stub: StubRecord,
        result: TaskQueueAutoscaleResult,
        config: TaskQueueAutoscalerConfig,
        decision: TaskQueueScaleDecision,
    ) -> None:
        if _should_persist_scale_event(
            self.services,
            target_kind=AutoscalerTargetKind.TaskQueue,
            stub=stub,
            decision=result.decision.value,
            desired_count=result.desired_containers,
            reason=result.reason,
            valid=result.valid,
            actions_taken=bool(result.actions),
        ):
            event_data: dict[str, JsonValue] = {
                "source": TASK_QUEUE_AUTOSCALER_SOURCE,
                "stub_id": stub.id,
                "workspace_id": stub.workspace_id,
                "current_containers": result.current_containers,
                "pending_containers": result.pending_containers,
                "desired_containers": result.desired_containers,
                "queue_length": result.queue_length,
                "running_tasks": result.running_tasks,
                "failed_containers": list(result.failed_containers),
                "decision": result.decision.value,
                "reason": result.reason,
                "valid": result.valid,
                "effective_max_containers": decision.effective_max_containers,
                "guardrails": result.guardrails,
                "actions": _autoscale_action_json_values(result.actions),
            }
            self.services.events.emit(
                TASK_QUEUE_SCALE_DECISION_ACTION,
                resource_type="stub",
                resource_id=stub.id,
                message="task queue autoscaler selected desired container count",
                data=event_data,
                workspace_id=stub.workspace_id,
            )
        _record_autoscaler_metrics(
            self.services,
            source=TASK_QUEUE_AUTOSCALER_SOURCE,
            stub=stub,
            kind=stub.kind.value,
            current_containers=result.current_containers,
            pending_containers=result.pending_containers,
            desired_containers=result.desired_containers,
            signal_name="queue_length",
            signal_value=result.queue_length,
            max_containers=decision.effective_max_containers or config.max_containers,
            pressure_saturated=_task_queue_pressure_saturated(
                result.queue_length,
                config,
                decision.effective_max_containers or config.max_containers,
            ),
            failed_container_count=len(result.failed_containers),
            decision=result.decision.value,
            reason=result.reason,
            guardrails=result.guardrails,
            actions=_autoscale_action_payloads(result.actions),
        )
        _record_autoscaler_state(
            self.services,
            source=TASK_QUEUE_AUTOSCALER_SOURCE,
            target_kind=AutoscalerTargetKind.TaskQueue,
            stub=stub,
            current_count=result.current_containers,
            desired_count=result.desired_containers,
            signal_name="queue_length",
            signal_value=result.queue_length,
            decision=result.decision.value,
            reason=result.reason,
            active=result.active,
            valid=result.valid,
            lock_acquired=result.lock_acquired,
            owner_lock_key=self._lock_key(stub),
            failed_container_count=len(result.failed_containers),
            last_sample={
                "queue_length": result.queue_length,
                "running_tasks": result.running_tasks,
                "current_containers": result.current_containers,
                "pending_containers": result.pending_containers,
                "guardrails": result.guardrails,
            },
            last_actions=_autoscale_action_payloads(result.actions),
        )

    def _lock_key(self, stub: StubRecord) -> str:
        return self.redis.key("autoscaling", "taskqueues", stub.workspace_id, stub.id, "lock")

    def _acquire_lock(self, key: str, token: str) -> bool:
        return bool(
            self.redis.set(
                key,
                token,
                nx=True,
                ex=TASK_QUEUE_AUTOSCALER_LOCK_TTL_SECONDS,
            )
        )

    def _release_lock(self, key: str, token: str) -> None:
        if _redis_text(self.redis.get(key)) == token:
            self.redis.delete(key)


@dataclass(slots=True)
class EndpointAutoscalingService:
    services: SchedulerServices
    redis: RedisClient
    endpoints: EndpointAutoscaleControl
    dispatches: EndpointAutoscalingDispatchReader

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[EndpointAutoscaleResult]:
        current_time = now or utc_now()
        stubs = [
            stub
            for stub in self.services.scheduler_workloads.list_stubs()
            if stub.kind in {StubKind.Endpoint, StubKind.Asgi}
            and stub.deployment_id
            and _endpoint_autoscaling_enabled(stub)
        ]
        results: list[EndpointAutoscaleResult] = []
        for stub in stubs[: max(limit, 0)]:
            token = token_urlsafe(16)
            lock_key = self._lock_key(stub)
            if not self._acquire_lock(lock_key, token):
                result = EndpointAutoscaleResult(
                    stub_id=stub.id,
                    workspace_id=stub.workspace_id,
                    decision=EndpointScaleDecisionKind.Hold,
                    reason="autoscaler lock already held",
                    lock_acquired=False,
                )
                _record_autoscaler_state(
                    self.services,
                    source=ENDPOINT_AUTOSCALER_SOURCE,
                    target_kind=AutoscalerTargetKind.Endpoint,
                    stub=stub,
                    current_count=result.current_containers,
                    desired_count=result.desired_containers,
                    decision=result.decision.value,
                    reason=result.reason,
                    active=result.active,
                    valid=result.valid,
                    lock_acquired=result.lock_acquired,
                    owner_lock_key=lock_key,
                )
                results.append(result)
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
    ) -> EndpointAutoscaleResult:
        current_time = now or utc_now()
        active = _deployment_active(self.services, stub)
        containers = _containers_for_stub(self.services, stub)
        active_containers = _active_containers(containers)
        current = len(active_containers)
        pending = _pending_container_count(containers)
        active_requests = self.dispatches.active_count(stub.id)
        config = _endpoint_autoscaler_config(stub.config)
        failed_containers = _recent_failed_container_ids(
            containers,
            now=current_time,
            window_seconds=_endpoint_failed_container_window_seconds(stub.config),
        )
        failure_threshold = _endpoint_failed_container_threshold(stub.config)
        failure_threshold_reached = (
            failure_threshold > 0 and len(failed_containers) >= failure_threshold
        )
        desired, reason = _endpoint_desired_containers(
            active_requests=active_requests,
            config=config,
        )
        decision = _endpoint_scale_kind(desired, current)
        if not active:
            desired = 0
            reason = "deployment inactive"
            decision = (
                EndpointScaleDecisionKind.ScaleDown
                if current > 0
                else EndpointScaleDecisionKind.Hold
            )
        elif failure_threshold_reached:
            desired = 0
            reason = "failed container threshold reached"
            decision = (
                EndpointScaleDecisionKind.ScaleDown
                if current > 0
                else EndpointScaleDecisionKind.Hold
            )
        guardrail = AutoscalerGuardrailPlan()
        if active and desired > current and decision is EndpointScaleDecisionKind.ScaleUp:
            guardrail = plan_autoscaler_start_guardrails(
                self.redis,
                stub=stub,
                current_count=current,
                desired_count=desired,
            )
            if guardrail.limited:
                desired = guardrail.desired_count
                reason = guardrail.reason
                decision = _endpoint_scale_kind(desired, current)
        actions: list[EndpointAutoscaleAction] = []
        delta = desired - current
        if delta > 0:
            actions.extend(self._scale_up(stub, delta))
        elif delta < 0:
            workspace_id = stub.workspace_id
            actions.extend(
                self._scale_down(
                    stub,
                    workspace_id,
                    containers,
                    -delta,
                    keep_warm_seconds=(
                        0 if stub.kind is StubKind.Sandbox else config.keep_warm_seconds
                    ),
                    now=current_time,
                )
            )
        result = EndpointAutoscaleResult(
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            active_requests=active_requests,
            current_containers=current,
            pending_containers=pending,
            desired_containers=desired,
            decision=decision,
            reason=reason,
            active=active,
            failed_containers=failed_containers,
            actions=actions,
            guardrails=guardrail.payload(),
        )
        self._emit_scale_event(stub, result, config)
        return result

    def _scale_up(self, stub: StubRecord, count: int) -> list[EndpointAutoscaleAction]:
        actions: list[EndpointAutoscaleAction] = []
        timeout = _endpoint_timeout_seconds(stub.config)
        for _ in range(count):
            try:
                response = self.endpoints.start_endpoint_serve(
                    StartEndpointServeRequest(stub_id=stub.id, timeout=timeout)
                )
            except DomainError as exc:
                actions.append(
                    EndpointAutoscaleAction(
                        container_id="",
                        action="scale-up-failed",
                        reason=exc.message,
                    )
                )
                break
            actions.append(
                EndpointAutoscaleAction(
                    container_id=response.container_id,
                    action="start",
                    reason="endpoint request pressure requires more containers",
                )
            )
        return actions

    def _scale_down(
        self,
        stub: StubRecord,
        workspace_id: str,
        containers: list[ContainerRecord],
        count: int,
        *,
        keep_warm_seconds: int,
        now: datetime,
    ) -> list[EndpointAutoscaleAction]:
        actions: list[EndpointAutoscaleAction] = []
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
            stopped = self.services.containers.stop(container.id)
            actions.append(
                EndpointAutoscaleAction(
                    container_id=stopped.id,
                    action="stop",
                    reason="endpoint request pressure no longer requires container",
                )
            )
        return actions

    def _emit_scale_event(
        self,
        stub: StubRecord,
        result: EndpointAutoscaleResult,
        config: EndpointAutoscalerConfig,
    ) -> None:
        if _should_persist_scale_event(
            self.services,
            target_kind=AutoscalerTargetKind.Endpoint,
            stub=stub,
            decision=result.decision.value,
            desired_count=result.desired_containers,
            reason=result.reason,
            valid=result.valid,
            actions_taken=bool(result.actions),
        ):
            event_data: dict[str, JsonValue] = {
                "source": ENDPOINT_AUTOSCALER_SOURCE,
                "stub_id": stub.id,
                "workspace_id": stub.workspace_id,
                "kind": stub.kind.value,
                "current_containers": result.current_containers,
                "pending_containers": result.pending_containers,
                "desired_containers": result.desired_containers,
                "active_requests": result.active_requests,
                "failed_containers": list(result.failed_containers),
                "decision": result.decision.value,
                "reason": result.reason,
                "valid": result.valid,
                "max_containers": config.max_containers,
                "min_containers": config.min_containers,
                "tasks_per_container": config.tasks_per_container,
                "guardrails": result.guardrails,
                "actions": _autoscale_action_json_values(result.actions),
            }
            self.services.events.emit(
                ENDPOINT_SCALE_DECISION_ACTION,
                resource_type="stub",
                resource_id=stub.id,
                message="endpoint autoscaler selected desired container count",
                data=event_data,
                workspace_id=stub.workspace_id,
            )
        _record_autoscaler_metrics(
            self.services,
            source=ENDPOINT_AUTOSCALER_SOURCE,
            stub=stub,
            kind=stub.kind.value,
            current_containers=result.current_containers,
            pending_containers=result.pending_containers,
            desired_containers=result.desired_containers,
            signal_name="active_requests",
            signal_value=result.active_requests,
            max_containers=config.max_containers,
            pressure_saturated=_endpoint_pressure_saturated(result.active_requests, config),
            failed_container_count=len(result.failed_containers),
            decision=result.decision.value,
            reason=result.reason,
            guardrails=result.guardrails,
            actions=_autoscale_action_payloads(result.actions),
        )
        _record_autoscaler_state(
            self.services,
            source=ENDPOINT_AUTOSCALER_SOURCE,
            target_kind=AutoscalerTargetKind.Endpoint,
            stub=stub,
            current_count=result.current_containers,
            desired_count=result.desired_containers,
            signal_name="active_requests",
            signal_value=result.active_requests,
            decision=result.decision.value,
            reason=result.reason,
            active=result.active,
            valid=result.valid,
            lock_acquired=result.lock_acquired,
            owner_lock_key=self._lock_key(stub),
            failed_container_count=len(result.failed_containers),
            last_sample={
                "active_requests": result.active_requests,
                "current_containers": result.current_containers,
                "pending_containers": result.pending_containers,
                "guardrails": result.guardrails,
            },
            last_actions=_autoscale_action_payloads(result.actions),
        )

    def _lock_key(self, stub: StubRecord) -> str:
        return self.redis.key("autoscaling", "endpoints", stub.workspace_id, stub.id, "lock")

    def _acquire_lock(self, key: str, token: str) -> bool:
        return bool(
            self.redis.set(
                key,
                token,
                nx=True,
                ex=ENDPOINT_AUTOSCALER_LOCK_TTL_SECONDS,
            )
        )

    def _release_lock(self, key: str, token: str) -> None:
        if _redis_text(self.redis.get(key)) == token:
            self.redis.delete(key)


@dataclass(slots=True)
class PodAutoscalingService:
    services: SchedulerServices
    redis: RedisClient
    pods: PodControl
    container_states: PodContainerStateReader | None = None

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[PodAutoscaleResult]:
        current_time = now or utc_now()
        stubs = [
            stub
            for stub in self.services.scheduler_workloads.list_stubs()
            if stub.kind in {StubKind.Pod, StubKind.Sandbox}
            and (stub.kind is StubKind.Sandbox or bool(stub.deployment_id))
            and _pod_autoscaling_enabled(stub)
        ]
        results: list[PodAutoscaleResult] = []
        for stub in stubs[: max(limit, 0)]:
            token = token_urlsafe(16)
            lock_key = self._lock_key(stub)
            if not self._acquire_lock(lock_key, token):
                result = PodAutoscaleResult(
                    stub_id=stub.id,
                    workspace_id=stub.workspace_id,
                    decision=PodScaleDecisionKind.Hold,
                    reason="autoscaler lock already held",
                    lock_acquired=False,
                )
                _record_autoscaler_state(
                    self.services,
                    source=POD_AUTOSCALER_SOURCE,
                    target_kind=AutoscalerTargetKind.Pod,
                    stub=stub,
                    current_count=result.current_containers,
                    desired_count=result.desired_containers,
                    decision=result.decision.value,
                    reason=result.reason,
                    active=result.active,
                    valid=result.valid,
                    lock_acquired=result.lock_acquired,
                    owner_lock_key=lock_key,
                )
                results.append(result)
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
    ) -> PodAutoscaleResult:
        current_time = now or utc_now()
        active = _deployment_active(self.services, stub)
        workspace_id = stub.workspace_id
        containers = _containers_for_stub(self.services, stub)
        active_containers, stale_containers = _pod_active_containers(
            containers,
            self.container_states,
        )
        stale_actions = self._stop_stale_containers(stale_containers)
        current = len(active_containers)
        pending = _pending_container_count(containers)
        total_connections = _pod_total_connections(self.redis, workspace_id, stub.id)
        config = _pod_autoscaler_config(stub)
        failed_containers = _recent_failed_container_ids(
            containers,
            now=current_time,
            window_seconds=_pod_failed_container_window_seconds(stub.config),
        )
        failure_threshold = _pod_failed_container_threshold(stub.config)
        failure_threshold_reached = (
            failure_threshold > 0 and len(failed_containers) >= failure_threshold
        )
        decision = decide_pod_scale(
            PodAutoscalerSample(
                current_containers=current,
                total_connections=total_connections,
            ),
            config,
        )
        desired = decision.desired_containers
        reason = decision.reason.value
        decision_kind = decision.decision
        if not active:
            desired = 0
            reason = "deployment inactive"
            decision_kind = (
                PodScaleDecisionKind.ScaleDown if current > 0 else PodScaleDecisionKind.Hold
            )
        elif failure_threshold_reached:
            desired = 0
            reason = "failed container threshold reached"
            decision_kind = (
                PodScaleDecisionKind.ScaleDown if current > 0 else PodScaleDecisionKind.Hold
            )

        guardrail = AutoscalerGuardrailPlan()
        if active and decision.valid and desired > current:
            guardrail = plan_autoscaler_start_guardrails(
                self.redis,
                stub=stub,
                current_count=current,
                desired_count=desired,
            )
            if guardrail.limited:
                desired = guardrail.desired_count
                reason = guardrail.reason
                decision_kind = _pod_scale_kind(desired, current)
        actions: list[PodAutoscaleAction] = list(stale_actions)
        delta = desired - current
        if delta > 0:
            actions.extend(self._scale_up(stub, delta))
        elif delta < 0:
            actions.extend(
                self._scale_down(
                    stub,
                    workspace_id,
                    active_containers,
                    -delta,
                    keep_warm_seconds=(
                        0 if stub.kind is StubKind.Sandbox else config.keep_warm_seconds
                    ),
                    active_instance=active and not failure_threshold_reached,
                    now=current_time,
                )
            )
        result = PodAutoscaleResult(
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            total_connections=total_connections,
            current_containers=current,
            pending_containers=pending,
            desired_containers=desired,
            decision=decision_kind,
            reason=reason,
            active=active,
            valid=decision.valid,
            failed_containers=failed_containers,
            actions=actions,
            guardrails=guardrail.payload(),
        )
        self._emit_scale_event(stub, result, config)
        return result

    def _stop_stale_containers(
        self,
        containers: list[ContainerRecord],
    ) -> list[PodAutoscaleAction]:
        actions: list[PodAutoscaleAction] = []
        for container in containers:
            stopped = self.services.containers.stop(container.id)
            actions.append(
                PodAutoscaleAction(
                    container_id=stopped.id,
                    action="recover-stale",
                    reason="scheduler state missing for running pod",
                )
            )
        return actions

    def _scale_up(self, stub: StubRecord, count: int) -> list[PodAutoscaleAction]:
        actions: list[PodAutoscaleAction] = []
        for _ in range(count):
            try:
                response = self.pods.create_pod(CreatePodRequest(stub_id=stub.id))
            except DomainError as exc:
                actions.append(
                    PodAutoscaleAction(
                        container_id="",
                        action="scale-up-failed",
                        reason=str(exc),
                    )
                )
                break
            if not response.container_id:
                actions.append(
                    PodAutoscaleAction(
                        container_id="",
                        action="scale-up-failed",
                        reason="pod create returned no container id",
                    )
                )
                break
            actions.append(
                PodAutoscaleAction(
                    container_id=response.container_id,
                    action="start",
                    reason="pod deployment desired capacity requires more containers",
                )
            )
        return actions

    def _scale_down(
        self,
        stub: StubRecord,
        workspace_id: str,
        containers: list[ContainerRecord],
        count: int,
        *,
        keep_warm_seconds: int,
        active_instance: bool,
        now: datetime,
    ) -> list[PodAutoscaleAction]:
        states = _pod_container_states(
            self.redis,
            workspace_id,
            stub,
            containers,
        )
        stop_plan = select_stoppable_pod_containers(
            states,
            active_instance=active_instance,
            keep_warm_seconds=keep_warm_seconds,
            keep_warm_lock_authoritative=stub.kind is StubKind.Sandbox,
            now_seconds=int(now.timestamp()),
        )
        actions: list[PodAutoscaleAction] = []
        for container_id in stop_plan.stoppable_container_ids[:count]:
            stopped = self.services.containers.stop(container_id)
            self.redis.delete(
                self.redis.key(pod_keep_warm_lock_key(workspace_id, stub.id, container_id))
            )
            actions.append(
                PodAutoscaleAction(
                    container_id=stopped.id,
                    action="stop",
                    reason="pod deployment desired capacity no longer requires container",
                )
            )
        return actions

    def _emit_scale_event(
        self,
        stub: StubRecord,
        result: PodAutoscaleResult,
        config: PodAutoscalerConfig,
    ) -> None:
        if _should_persist_scale_event(
            self.services,
            target_kind=AutoscalerTargetKind.Pod,
            stub=stub,
            decision=result.decision.value,
            desired_count=result.desired_containers,
            reason=result.reason,
            valid=result.valid,
            actions_taken=bool(result.actions),
        ):
            event_data: dict[str, JsonValue] = {
                "source": POD_AUTOSCALER_SOURCE,
                "stub_id": stub.id,
                "workspace_id": stub.workspace_id,
                "kind": stub.kind.value,
                "current_containers": result.current_containers,
                "pending_containers": result.pending_containers,
                "desired_containers": result.desired_containers,
                "total_connections": result.total_connections,
                "failed_containers": list(result.failed_containers),
                "decision": result.decision.value,
                "reason": result.reason,
                "valid": result.valid,
                "max_containers": config.max_containers,
                "min_containers": config.min_containers,
                "guardrails": result.guardrails,
                "actions": _autoscale_action_json_values(result.actions),
            }
            self.services.events.emit(
                POD_SCALE_DECISION_ACTION,
                resource_type="stub",
                resource_id=stub.id,
                message="pod autoscaler selected desired container count",
                data=event_data,
                workspace_id=stub.workspace_id,
            )
        _record_autoscaler_metrics(
            self.services,
            source=POD_AUTOSCALER_SOURCE,
            stub=stub,
            kind=stub.kind.value,
            current_containers=result.current_containers,
            pending_containers=result.pending_containers,
            desired_containers=result.desired_containers,
            signal_name="total_connections",
            signal_value=result.total_connections,
            max_containers=config.max_containers,
            pressure_saturated=_pod_pressure_saturated(result.total_connections, config),
            failed_container_count=len(result.failed_containers),
            decision=result.decision.value,
            reason=result.reason,
            guardrails=result.guardrails,
            actions=_autoscale_action_payloads(result.actions),
        )
        _record_autoscaler_state(
            self.services,
            source=POD_AUTOSCALER_SOURCE,
            target_kind=AutoscalerTargetKind.Pod,
            stub=stub,
            current_count=result.current_containers,
            desired_count=result.desired_containers,
            signal_name="total_connections",
            signal_value=result.total_connections,
            decision=result.decision.value,
            reason=result.reason,
            active=result.active,
            valid=result.valid,
            lock_acquired=result.lock_acquired,
            owner_lock_key=self._lock_key(stub),
            failed_container_count=len(result.failed_containers),
            last_sample={
                "total_connections": result.total_connections,
                "current_containers": result.current_containers,
                "pending_containers": result.pending_containers,
                "guardrails": result.guardrails,
            },
            last_actions=_autoscale_action_payloads(result.actions),
        )

    def _lock_key(self, stub: StubRecord) -> str:
        return self.redis.key("autoscaling", "pods", stub.workspace_id, stub.id, "lock")

    def _acquire_lock(self, key: str, token: str) -> bool:
        return bool(
            self.redis.set(
                key,
                token,
                nx=True,
                ex=POD_AUTOSCALER_LOCK_TTL_SECONDS,
            )
        )

    def _release_lock(self, key: str, token: str) -> None:
        if _redis_text(self.redis.get(key)) == token:
            self.redis.delete(key)


class EndpointAutoscalerConfig(ContractModel):
    min_containers: int = 0
    max_containers: int = 1
    tasks_per_container: int = 1
    keep_warm_seconds: int = 0


def _task_queue_autoscaling_enabled(stub: StubRecord) -> bool:
    raw = stub.config.metadata.get("autoscaling_enabled", True)
    return raw is not False


def _endpoint_autoscaling_enabled(stub: StubRecord) -> bool:
    raw = stub.config.metadata.get("autoscaling_enabled", True)
    return raw is not False


def _pod_autoscaling_enabled(stub: StubRecord) -> bool:
    raw = stub.config.metadata.get("autoscaling_enabled", True)
    return raw is not False


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


def _task_queue_pressure_saturated(
    queue_length: int,
    config: TaskQueueAutoscalerConfig,
    max_containers: int,
) -> bool:
    target_messages = max(max_containers, 0) * max(config.tasks_per_container, 1)
    return target_messages > 0 and queue_length > target_messages


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


def _task_queue_containers(
    services: SchedulerServices,
    stub: StubRecord,
) -> list[ContainerRecord]:
    return _containers_for_stub(services, stub)


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


def _queue_length(services: SchedulerServices, stub: StubRecord) -> int:
    queue_name = f"taskqueue:{stub.id}"
    return services.collections.queue_depth(queue_name, workspace_id=stub.workspace_id)


def _running_task_count(
    redis: RedisClient,
    workspace_id: str,
    stub: StubRecord,
    containers: list[ContainerRecord],
) -> int:
    count = 0
    for container in containers:
        if container.status not in {ContainerStatus.Pending, ContainerStatus.Running}:
            continue
        index_key = _redis_key(
            redis,
            task_queue_running_lock_index_key(workspace_id, stub.id, container.id),
        )
        task_ids = [_redis_text(item) for item in redis.set_members(index_key)]
        for task_id in task_ids:
            lock_key = _redis_key(
                redis,
                task_queue_running_lock_key(workspace_id, stub.id, container.id, task_id),
            )
            if int(redis.exists(lock_key) or 0) > 0:
                count += 1
    return count


def _stoppable_task_queue_containers(
    redis: RedisClient,
    workspace_id: str,
    stub: StubRecord,
    containers: list[ContainerRecord],
) -> list[ContainerRecord]:
    candidates = [
        container
        for container in containers
        if container.status is ContainerStatus.Running
        and _scheduler_status(redis, container.id) is not SchedulerContainerStatus.Stopping
        and not _container_has_keep_warm_lock(redis, workspace_id, stub.id, container.id)
        and not _container_has_processing_lock(redis, workspace_id, stub.id, container.id)
        and not _container_has_running_task(redis, workspace_id, stub.id, container.id)
    ]
    candidates.sort(key=lambda container: container.created_at, reverse=True)
    return candidates


def _container_has_keep_warm_lock(
    redis: RedisClient,
    workspace_id: str,
    stub_id: str,
    container_id: str,
) -> bool:
    return _exists(
        redis,
        task_queue_keep_warm_lock_key(workspace_id, stub_id, container_id),
    )


def _container_has_processing_lock(
    redis: RedisClient,
    workspace_id: str,
    stub_id: str,
    container_id: str,
) -> bool:
    return _exists(
        redis,
        task_queue_processing_lock_key(workspace_id, stub_id, container_id),
    )


def _container_has_running_task(
    redis: RedisClient,
    workspace_id: str,
    stub_id: str,
    container_id: str,
) -> bool:
    index_key = _redis_key(
        redis,
        task_queue_running_lock_index_key(workspace_id, stub_id, container_id),
    )
    task_ids = [_redis_text(item) for item in redis.set_members(index_key)]
    for task_id in task_ids:
        if _exists(
            redis,
            task_queue_running_lock_key(workspace_id, stub_id, container_id, task_id),
        ):
            return True
        redis.set_remove(index_key, task_id)
    return False


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


def _recent_failed_task_queue_container_ids(
    containers: list[ContainerRecord],
    *,
    now: datetime,
    window_seconds: int,
) -> list[str]:
    return _recent_failed_container_ids(containers, now=now, window_seconds=window_seconds)


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


def _endpoint_scale_kind(desired: int, current: int) -> EndpointScaleDecisionKind:
    if current < 0 or desired < 0:
        return EndpointScaleDecisionKind.Invalid
    if desired > current:
        return EndpointScaleDecisionKind.ScaleUp
    if desired < current:
        return EndpointScaleDecisionKind.ScaleDown
    return EndpointScaleDecisionKind.Hold


def _task_queue_scale_kind(desired: int, current: int) -> TaskQueueScaleDecisionKind:
    if current < 0 or desired < 0:
        return TaskQueueScaleDecisionKind.Invalid
    if desired > current:
        return TaskQueueScaleDecisionKind.ScaleUp
    if desired < current:
        return TaskQueueScaleDecisionKind.ScaleDown
    return TaskQueueScaleDecisionKind.Hold


def _pod_scale_kind(desired: int, current: int) -> PodScaleDecisionKind:
    if current < 0 or desired < 0:
        return PodScaleDecisionKind.Invalid
    if desired > current:
        return PodScaleDecisionKind.ScaleUp
    if desired < current:
        return PodScaleDecisionKind.ScaleDown
    return PodScaleDecisionKind.Hold


def _task_queue_autoscaler_config(config: StubConfig) -> TaskQueueAutoscalerConfig:
    autoscaler = config.autoscaler
    return TaskQueueAutoscalerConfig(
        tasks_per_container=autoscaler.tasks_per_container,
        max_containers=autoscaler.max_containers,
    )


def _failed_container_threshold(config: StubConfig) -> int:
    return _first_configured_non_negative(
        config.autoscaler.failed_container_threshold,
        config.autoscaler.max_failed_containers,
        config.autoscaler.failure_threshold,
        default=TASK_QUEUE_AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD,
    )


def _failed_container_window_seconds(config: StubConfig) -> int:
    return _first_configured_non_negative(
        config.autoscaler.failed_container_window_seconds,
        config.autoscaler.failure_window_seconds,
        default=TASK_QUEUE_AUTOSCALER_DEFAULT_FAILURE_WINDOW_SECONDS,
    )


def _task_queue_timeout_seconds(config: StubConfig) -> int:
    return (
        int(config.task_policy.timeout_seconds)
        or int(config.task_policy.timeout)
        or int(config.runtime.timeout_seconds or 0)
        or TASK_QUEUE_AUTOSCALER_DEFAULT_TIMEOUT_SECONDS
    )


def _endpoint_failed_container_threshold(config: StubConfig) -> int:
    return _first_configured_non_negative(
        config.autoscaler.failed_container_threshold,
        config.autoscaler.max_failed_containers,
        config.autoscaler.failure_threshold,
        default=ENDPOINT_AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD,
    )


def _endpoint_failed_container_window_seconds(config: StubConfig) -> int:
    return _first_configured_non_negative(
        config.autoscaler.failed_container_window_seconds,
        config.autoscaler.failure_window_seconds,
        default=ENDPOINT_AUTOSCALER_DEFAULT_FAILURE_WINDOW_SECONDS,
    )


def _pod_failed_container_threshold(config: StubConfig) -> int:
    return _first_configured_non_negative(
        config.autoscaler.failed_container_threshold,
        config.autoscaler.max_failed_containers,
        config.autoscaler.failure_threshold,
        default=POD_AUTOSCALER_DEFAULT_FAILED_CONTAINER_THRESHOLD,
    )


def _pod_failed_container_window_seconds(config: StubConfig) -> int:
    return _first_configured_non_negative(
        config.autoscaler.failed_container_window_seconds,
        config.autoscaler.failure_window_seconds,
        default=POD_AUTOSCALER_DEFAULT_FAILURE_WINDOW_SECONDS,
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
    "EndpointAutoscaleAction",
    "EndpointAutoscaleResult",
    "EndpointAutoscalingService",
    "PodAutoscaleAction",
    "PodAutoscaleResult",
    "PodAutoscalingService",
    "SchedulerServices",
    "TaskQueueAutoscaleAction",
    "TaskQueueAutoscaleResult",
    "TaskQueueAutoscalingService",
]
