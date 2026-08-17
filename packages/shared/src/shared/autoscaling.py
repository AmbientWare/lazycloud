from __future__ import annotations

from typing import Literal

from pydantic import Field, computed_field, field_validator, model_validator

from shared.containers import ContainerStatus
from shared.contracts import ContractModel
from shared.enums import StringEnum


class QueueDepthAutoscaler(ContractModel):
    type: Literal["queue_depth"] = "queue_depth"
    min_containers: int = Field(default=0, ge=0)
    max_containers: int = Field(default=1, ge=0)
    tasks_per_container: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def minimum_cannot_exceed_maximum(self) -> QueueDepthAutoscaler:
        if self.min_containers > self.max_containers:
            msg = "min_containers cannot exceed max_containers"
            raise ValueError(msg)
        return self


def function_container_ceiling(max_containers: int) -> int:
    """The most containers one function stub may hold at once.

    Floored at one because `max_containers` defaults to a policy nobody wrote,
    not to a refusal: a function declared without an autoscaler still has to be
    able to run. Every start of a function container is refused past this, so
    the number has to mean the same thing to the autoscaler deciding depth and
    to the reservation that grants it — two readings of it would disagree, and
    the disagreement would arrive as a bill.
    """

    return max(max_containers, 1)


class PodStubType(StringEnum):
    Pod = "pod"
    PodDeployment = "pod/deployment"
    PodRun = "pod/run"
    Sandbox = "sandbox"


class PodScaleDecisionKind(StringEnum):
    ScaleUp = "scale-up"
    ScaleDown = "scale-down"
    Hold = "hold"
    Invalid = "invalid"


class PodScaleReason(StringEnum):
    InvalidSample = "invalid-sample"
    OneShotNoContainers = "one-shot-no-containers"
    OneShotRunning = "one-shot-running"
    OneShotKeepWarmDrain = "one-shot-keep-warm-drain"
    DeploymentIdle = "deployment-idle"
    DeploymentConnectionsActive = "deployment-connections-active"


class PodAutoscalerSample(ContractModel):
    current_containers: int = 0
    total_connections: int = 0

    @field_validator("current_containers", "total_connections")
    @classmethod
    def allow_unknown_or_non_negative(cls, value: int) -> int:
        if value < -1:
            msg = "pod autoscaler sample values must be -1 for unknown or non-negative"
            raise ValueError(msg)
        return value

    @computed_field
    @property
    def valid(self) -> bool:
        return self.current_containers >= 0 and self.total_connections >= 0


class PodAutoscalerConfig(ContractModel):
    stub_type: PodStubType = PodStubType.PodDeployment
    min_containers: int = Field(default=0, ge=0)
    max_containers: int = Field(default=1, ge=0)
    keep_warm_seconds: int = Field(default=0, ge=-1)

    @model_validator(mode="after")
    def minimum_cannot_exceed_maximum(self) -> PodAutoscalerConfig:
        if self.min_containers > self.max_containers:
            msg = "min_containers cannot exceed max_containers"
            raise ValueError(msg)
        return self


class PodScaleDecision(ContractModel):
    decision: PodScaleDecisionKind
    reason: PodScaleReason
    desired_containers: int = Field(default=0, ge=0)
    valid: bool = True
    sample: PodAutoscalerSample


class PodContainerState(ContractModel):
    container_id: str
    status: ContainerStatus = ContainerStatus.Running
    started_at_seconds: int = 0
    keep_warm_lock_present: bool = False
    active_connections: int = Field(default=0, ge=0)


class PodContainerSkipReason(StringEnum):
    Pending = "pending"
    Stopping = "stopping"
    KeepWarmWindow = "keep-warm-window"
    KeepWarmLock = "keep-warm-lock"
    ActiveConnections = "active-connections"


class PodContainerStopSkip(ContractModel):
    container_id: str
    reason: PodContainerSkipReason


class PodStopPlan(ContractModel):
    stoppable_container_ids: list[str]
    skipped: list[PodContainerStopSkip] = Field(default_factory=list)


class BacklogScaleDecisionKind(StringEnum):
    ScaleUp = "scale-up"
    ScaleDown = "scale-down"
    Hold = "hold"
    Invalid = "invalid"


class BacklogScaleReason(StringEnum):
    InvalidSample = "invalid-sample"
    QueueEmpty = "queue-empty"
    QueuePending = "queue-pending"
    ReplicaLimit = "replica-limit"


class BacklogAutoscalerSample(ContractModel):
    queue_length: int = 0
    running_tasks: int = 0
    current_containers: int = 0
    task_duration_ms: float = 0.0

    @field_validator("queue_length", "running_tasks", "current_containers", "task_duration_ms")
    @classmethod
    def allow_unknown_or_non_negative(cls, value: int | float) -> int | float:
        if value < -1:
            msg = "backlog autoscaler sample values must be -1 for unknown or non-negative"
            raise ValueError(msg)
        return value

    @computed_field
    @property
    def valid(self) -> bool:
        return (
            self.queue_length >= 0
            and self.running_tasks >= 0
            and self.current_containers >= 0
            and self.task_duration_ms >= 0
        )


class BacklogAutoscalerConfig(ContractModel):
    tasks_per_container: int = Field(default=1, ge=1)
    max_containers: int = Field(default=1, ge=0)
    gateway_max_replicas: int | None = Field(default=None, ge=0)

    @computed_field
    @property
    def effective_max_containers(self) -> int:
        if self.gateway_max_replicas is None:
            return self.max_containers
        return min(self.max_containers, self.gateway_max_replicas)


class BacklogScaleDecision(ContractModel):
    decision: BacklogScaleDecisionKind
    reason: BacklogScaleReason
    desired_containers: int = Field(default=0, ge=0)
    valid: bool = True
    sample: BacklogAutoscalerSample
    effective_max_containers: int | None = None


def decide_pod_scale(
    sample: PodAutoscalerSample,
    config: PodAutoscalerConfig | None = None,
) -> PodScaleDecision:
    autoscaler_config = config or PodAutoscalerConfig()
    if not sample.valid:
        return PodScaleDecision(
            decision=PodScaleDecisionKind.Invalid,
            reason=PodScaleReason.InvalidSample,
            desired_containers=0,
            valid=False,
            sample=sample,
        )
    if autoscaler_config.stub_type in {PodStubType.PodRun, PodStubType.Sandbox}:
        if sample.current_containers == 0:
            desired = 0
            reason = PodScaleReason.OneShotNoContainers
        elif autoscaler_config.keep_warm_seconds >= 0:
            desired = 0
            reason = PodScaleReason.OneShotKeepWarmDrain
        else:
            desired = 1
            reason = PodScaleReason.OneShotRunning
    elif sample.total_connections == 0:
        desired = autoscaler_config.min_containers
        reason = PodScaleReason.DeploymentIdle
    else:
        desired = autoscaler_config.max_containers
        reason = PodScaleReason.DeploymentConnectionsActive
    return PodScaleDecision(
        decision=_pod_scale_kind(desired, sample.current_containers),
        reason=reason,
        desired_containers=desired,
        sample=sample,
    )


def select_stoppable_pod_containers(
    containers: list[PodContainerState],
    *,
    active_instance: bool = True,
    keep_warm_seconds: int = 0,
    keep_warm_lock_authoritative: bool = False,
    now_seconds: int = 0,
) -> PodStopPlan:
    stoppable: list[str] = []
    skipped: list[PodContainerStopSkip] = []
    for container in containers:
        reason = _pod_stop_skip_reason(
            container,
            active_instance=active_instance,
            keep_warm_seconds=keep_warm_seconds,
            keep_warm_lock_authoritative=keep_warm_lock_authoritative,
            now_seconds=now_seconds,
        )
        if reason is None:
            stoppable.append(container.container_id)
        else:
            skipped.append(PodContainerStopSkip(container_id=container.container_id, reason=reason))
    return PodStopPlan(stoppable_container_ids=stoppable, skipped=skipped)


def decide_backlog_scale(
    sample: BacklogAutoscalerSample,
    config: BacklogAutoscalerConfig | None = None,
) -> BacklogScaleDecision:
    autoscaler_config = config or BacklogAutoscalerConfig()
    if not sample.valid:
        return BacklogScaleDecision(
            decision=BacklogScaleDecisionKind.Invalid,
            reason=BacklogScaleReason.InvalidSample,
            desired_containers=0,
            valid=False,
            sample=sample,
            effective_max_containers=autoscaler_config.effective_max_containers,
        )
    if sample.queue_length == 0:
        desired = 0
        reason = BacklogScaleReason.QueueEmpty
    else:
        required = (sample.queue_length + autoscaler_config.tasks_per_container - 1) // (
            autoscaler_config.tasks_per_container
        )
        desired = min(required, autoscaler_config.effective_max_containers)
        reason = (
            BacklogScaleReason.ReplicaLimit
            if desired < required
            else BacklogScaleReason.QueuePending
        )
    return BacklogScaleDecision(
        decision=_backlog_scale_kind(desired, sample.current_containers),
        reason=reason,
        desired_containers=desired,
        sample=sample,
        effective_max_containers=autoscaler_config.effective_max_containers,
    )


def _pod_scale_kind(desired: int, current: int) -> PodScaleDecisionKind:
    if desired > current:
        return PodScaleDecisionKind.ScaleUp
    if desired < current:
        return PodScaleDecisionKind.ScaleDown
    return PodScaleDecisionKind.Hold


def _backlog_scale_kind(desired: int, current: int) -> BacklogScaleDecisionKind:
    if desired > current:
        return BacklogScaleDecisionKind.ScaleUp
    if desired < current:
        return BacklogScaleDecisionKind.ScaleDown
    return BacklogScaleDecisionKind.Hold


def _pod_stop_skip_reason(
    container: PodContainerState,
    *,
    active_instance: bool,
    keep_warm_seconds: int,
    keep_warm_lock_authoritative: bool,
    now_seconds: int,
) -> PodContainerSkipReason | None:
    if container.status is ContainerStatus.Pending:
        return PodContainerSkipReason.Pending
    if container.status is ContainerStatus.Stopped:
        return PodContainerSkipReason.Stopping
    if not active_instance:
        return None
    if (
        keep_warm_seconds > 0
        and container.started_at_seconds > 0
        and now_seconds < container.started_at_seconds + keep_warm_seconds
    ):
        return PodContainerSkipReason.KeepWarmWindow
    if (
        keep_warm_lock_authoritative or keep_warm_seconds != 0
    ) and container.keep_warm_lock_present:
        return PodContainerSkipReason.KeepWarmLock
    if container.active_connections > 0:
        return PodContainerSkipReason.ActiveConnections
    return None


__all__ = [
    "BacklogAutoscalerConfig",
    "BacklogAutoscalerSample",
    "BacklogScaleDecision",
    "BacklogScaleDecisionKind",
    "BacklogScaleReason",
    "PodAutoscalerConfig",
    "PodAutoscalerSample",
    "PodContainerSkipReason",
    "PodContainerState",
    "PodContainerStopSkip",
    "PodScaleDecision",
    "PodScaleDecisionKind",
    "PodScaleReason",
    "PodStopPlan",
    "PodStubType",
    "QueueDepthAutoscaler",
    "decide_backlog_scale",
    "decide_pod_scale",
    "function_container_ceiling",
    "select_stoppable_pod_containers",
]
