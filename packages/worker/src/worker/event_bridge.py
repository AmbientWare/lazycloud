from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import JsonValue
from shared.contracts import ContractModel

from worker.events import (
    StopContainerReason,
    WorkerBuildCancelRegistry,
    WorkerBuildCancelResult,
    WorkerStreamDecisionAction,
    WorkerStreamEvent,
    WorkerStreamEventDecision,
    WorkerStreamEventKind,
    decide_worker_stream_event,
    normalize_stop_reason,
)
from worker.source_cache_cleanup import WorkerSourceCacheReconcileResult

STOP_CONTAINER_EVENT_TYPE = "STOP_CONTAINER"
STOP_BUILD_EVENT_TYPE = "STOP_BUILD"
PURGE_SOURCE_CACHE_EVENT_TYPE = "PURGE_SOURCE_CACHE"


class WorkerEventBusEnvelope(Protocol):
    type: str
    args: dict[str, JsonValue]


class WorkerEventBridgeStatus(StrEnum):
    Converted = "converted"
    Invalid = "invalid"
    Unsupported = "unsupported"


class WorkerEventHandlingStatus(StrEnum):
    Ignored = "ignored"
    StoppedContainer = "stopped-container"
    CancelledBuild = "cancelled-build"
    ReconciledSourceCache = "reconciled-source-cache"
    Warn = "warn"
    Error = "error"


class WorkerEventContainerStopper(Protocol):
    def stop_container(
        self,
        container_id: str,
        *,
        force: bool,
        reason: StopContainerReason = StopContainerReason.Unknown,
    ) -> None: ...


class WorkerSourceCacheReconciler(Protocol):
    def reconcile_source_cache(self) -> WorkerSourceCacheReconcileResult: ...


class WorkerEventAcknowledger(Protocol):
    def acknowledge_worker_event(self, event_id: str, worker_id: str) -> None: ...


class WorkerEventBridgePlan(ContractModel):
    status: WorkerEventBridgeStatus
    event_id: str = ""
    event: WorkerStreamEvent | None = None
    reason: str = ""

    @property
    def converted(self) -> bool:
        return self.status is WorkerEventBridgeStatus.Converted


class WorkerEventHandlingResult(ContractModel):
    status: WorkerEventHandlingStatus
    event_id: str = ""
    decision: WorkerStreamEventDecision | None = None
    container_id: str = ""
    force: bool = False
    cancel_result: WorkerBuildCancelResult | None = None
    source_cache_reconcile: WorkerSourceCacheReconcileResult | None = None
    error_message: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not WorkerEventHandlingStatus.Error


@dataclass(slots=True)
class WorkerStreamEventHandler:
    container_stopper: WorkerEventContainerStopper | None = None
    build_cancels: WorkerBuildCancelRegistry | None = None
    source_cache: WorkerSourceCacheReconciler | None = None
    acknowledger: WorkerEventAcknowledger | None = None
    worker_id: str = ""

    def handle(self, event: WorkerStreamEvent | None) -> WorkerEventHandlingResult:
        decision = decide_worker_stream_event(event)
        if decision.action is WorkerStreamDecisionAction.Ignore:
            return WorkerEventHandlingResult(
                status=WorkerEventHandlingStatus.Ignored,
                event_id=decision.event_id,
                decision=decision,
                reason=decision.reason,
            )
        if decision.action is WorkerStreamDecisionAction.Warn:
            return WorkerEventHandlingResult(
                status=WorkerEventHandlingStatus.Warn,
                event_id=decision.event_id,
                decision=decision,
                reason=decision.reason,
            )
        if decision.action is WorkerStreamDecisionAction.StopContainer:
            return self._stop_container(decision)
        if decision.action is WorkerStreamDecisionAction.CancelBuild:
            return self._cancel_build(decision)
        if decision.action is WorkerStreamDecisionAction.PurgeSourceCache:
            return self._purge_source_cache(decision)
        return WorkerEventHandlingResult(
            status=WorkerEventHandlingStatus.Error,
            event_id=decision.event_id,
            decision=decision,
            error_message=f"unsupported worker event action: {decision.action.value}",
            reason=decision.reason,
        )

    def _stop_container(
        self,
        decision: WorkerStreamEventDecision,
    ) -> WorkerEventHandlingResult:
        plan = decision.stop_container
        if plan is None:
            return WorkerEventHandlingResult(
                status=WorkerEventHandlingStatus.Error,
                event_id=decision.event_id,
                decision=decision,
                error_message="stop container plan is missing",
                reason=decision.reason,
            )
        if self.container_stopper is None:
            return WorkerEventHandlingResult(
                status=WorkerEventHandlingStatus.Error,
                event_id=decision.event_id,
                decision=decision,
                container_id=plan.container_id,
                force=plan.force,
                error_message="container stopper is not configured",
                reason=decision.reason,
            )
        try:
            self.container_stopper.stop_container(
                plan.container_id,
                force=plan.force,
                reason=plan.reason,
            )
            if self.acknowledger is None or not self.worker_id:
                raise RuntimeError("worker event acknowledger is not configured")
            self.acknowledger.acknowledge_worker_event(decision.event_id, self.worker_id)
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            return WorkerEventHandlingResult(
                status=WorkerEventHandlingStatus.Error,
                event_id=decision.event_id,
                decision=decision,
                container_id=plan.container_id,
                force=plan.force,
                error_message=f"{type(exc).__name__}: {exc}",
                reason=decision.reason,
            )
        return WorkerEventHandlingResult(
            status=WorkerEventHandlingStatus.StoppedContainer,
            event_id=decision.event_id,
            decision=decision,
            container_id=plan.container_id,
            force=plan.force,
            reason=decision.reason,
        )

    def _cancel_build(
        self,
        decision: WorkerStreamEventDecision,
    ) -> WorkerEventHandlingResult:
        container_id = decision.cancel_build_container_id
        if self.build_cancels is None:
            return WorkerEventHandlingResult(
                status=WorkerEventHandlingStatus.CancelledBuild,
                event_id=decision.event_id,
                decision=decision,
                container_id=container_id,
                reason="build cancel registry is not configured",
            )
        cancel_result = self.build_cancels.cancel(container_id)
        return WorkerEventHandlingResult(
            status=WorkerEventHandlingStatus.CancelledBuild,
            event_id=decision.event_id,
            decision=decision,
            container_id=container_id,
            cancel_result=cancel_result,
            reason=cancel_result.reason,
        )

    def _purge_source_cache(
        self,
        decision: WorkerStreamEventDecision,
    ) -> WorkerEventHandlingResult:
        if self.source_cache is None:
            return WorkerEventHandlingResult(
                status=WorkerEventHandlingStatus.Error,
                event_id=decision.event_id,
                decision=decision,
                error_message="source cache purger is not configured",
                reason=decision.reason,
            )
        try:
            reconcile = self.source_cache.reconcile_source_cache()
        except Exception as exc:  # pragma: no cover - defensive owner boundary
            return WorkerEventHandlingResult(
                status=WorkerEventHandlingStatus.Error,
                event_id=decision.event_id,
                decision=decision,
                error_message=f"{type(exc).__name__}: {exc}",
                reason=decision.reason,
            )
        return WorkerEventHandlingResult(
            status=WorkerEventHandlingStatus.ReconciledSourceCache,
            event_id=decision.event_id,
            decision=decision,
            source_cache_reconcile=reconcile,
            reason=decision.reason,
        )


def worker_stream_event_from_bus_event(
    *,
    event_id: str,
    event: WorkerEventBusEnvelope | None,
) -> WorkerEventBridgePlan:
    if event is None:
        return WorkerEventBridgePlan(
            status=WorkerEventBridgeStatus.Invalid,
            event_id=event_id,
            reason="event is required",
        )
    if event.type == STOP_CONTAINER_EVENT_TYPE:
        return _stop_container_event(event_id, event)
    if event.type == STOP_BUILD_EVENT_TYPE:
        return _stop_build_event(event_id, event)
    if event.type == PURGE_SOURCE_CACHE_EVENT_TYPE:
        return _purge_source_cache_event(event_id)
    return WorkerEventBridgePlan(
        status=WorkerEventBridgeStatus.Unsupported,
        event_id=event_id,
        reason=f"unsupported worker event type: {event.type}",
    )


def _stop_container_event(
    event_id: str,
    event: WorkerEventBusEnvelope,
) -> WorkerEventBridgePlan:
    container_id = _string_arg(event, "container_id")
    if not container_id:
        return WorkerEventBridgePlan(
            status=WorkerEventBridgeStatus.Invalid,
            event_id=event_id,
            reason="missing container_id",
        )
    return WorkerEventBridgePlan(
        status=WorkerEventBridgeStatus.Converted,
        event_id=event_id,
        event=WorkerStreamEvent(
            event_id=event_id,
            kind=WorkerStreamEventKind.StopContainer,
            container_id=container_id,
            force=_bool_arg(event, "force"),
            reason=_stop_reason_arg(event),
        ),
        reason="converted stop container event",
    )


def _stop_build_event(event_id: str, event: WorkerEventBusEnvelope) -> WorkerEventBridgePlan:
    container_id = _string_arg(event, "container_id")
    if not container_id:
        return WorkerEventBridgePlan(
            status=WorkerEventBridgeStatus.Invalid,
            event_id=event_id,
            reason="missing container_id",
        )
    return WorkerEventBridgePlan(
        status=WorkerEventBridgeStatus.Converted,
        event_id=event_id,
        event=WorkerStreamEvent(
            event_id=event_id,
            kind=WorkerStreamEventKind.StopBuild,
            container_id=container_id,
        ),
        reason="converted stop build event",
    )


def _purge_source_cache_event(event_id: str) -> WorkerEventBridgePlan:
    return WorkerEventBridgePlan(
        status=WorkerEventBridgeStatus.Converted,
        event_id=event_id,
        event=WorkerStreamEvent(
            event_id=event_id,
            kind=WorkerStreamEventKind.PurgeSourceCache,
        ),
        reason="converted source cache reconciliation wake",
    )


def _string_arg(event: WorkerEventBusEnvelope, key: str) -> str:
    value = event.args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _bool_arg(event: WorkerEventBusEnvelope, key: str) -> bool:
    value = event.args.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def _stop_reason_arg(event: WorkerEventBusEnvelope) -> StopContainerReason:
    value = event.args.get("reason")
    return normalize_stop_reason(value if isinstance(value, str) else None)
