from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import Field
from shared.contracts import ContractModel

from images.building import (
    ImageBuildCancellationPlan,
    ImageBuildLifecycleAction,
    ImageBuildSessionPlan,
    plan_image_build_cancellation,
)


class ImageBuildContainerLifecycleStatus(StrEnum):
    Skipped = "skipped"
    Complete = "complete"
    Error = "error"


class ImageBuildContainerLifecycleOperation(StrEnum):
    StartSession = "start-session"
    CancelSession = "cancel-session"


class ImageBuildStopEventResult(ContractModel):
    status: ImageBuildContainerLifecycleStatus
    container_id: str
    event_ids: list[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not ImageBuildContainerLifecycleStatus.Error


class ImageBuildContainerLifecycleResult(ContractModel):
    status: ImageBuildContainerLifecycleStatus
    operation: ImageBuildContainerLifecycleOperation
    container_id: str = ""
    actions: list[ImageBuildLifecycleAction] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    cache_metadata: dict[str, str] = Field(default_factory=dict)
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is ImageBuildContainerLifecycleStatus.Complete


class ImageBuildContainerTtlStore(Protocol):
    def set_build_container_ttl(self, container_id: str, ttl_seconds: int) -> bool: ...


class ImageBuildStopEventPublisher(Protocol):
    def send_stop_build(self, container_id: str) -> ImageBuildStopEventResult: ...


class ImageBuildContainerStateStore(Protocol):
    def delete_pending_build_container(self, container_id: str) -> bool: ...

    def mark_build_container_stopping(self, container_id: str, ttl_seconds: int) -> bool: ...


class ImageBuildContainerKiller(Protocol):
    def kill_build_container(self, container_id: str) -> bool: ...


@dataclass(slots=True)
class ImageBuildContainerLifecycleService:
    ttl_store: ImageBuildContainerTtlStore | None = None
    stop_publisher: ImageBuildStopEventPublisher | None = None
    state_store: ImageBuildContainerStateStore | None = None
    killer: ImageBuildContainerKiller | None = None

    def start_session(
        self,
        session: ImageBuildSessionPlan,
    ) -> ImageBuildContainerLifecycleResult:
        metadata = image_build_lifecycle_session_metadata(session)
        if not session.build_container_required:
            return ImageBuildContainerLifecycleResult(
                status=ImageBuildContainerLifecycleStatus.Skipped,
                operation=ImageBuildContainerLifecycleOperation.StartSession,
                container_id=session.container_id,
                cache_metadata=metadata,
                reason="image build does not require a build container",
            )
        if self.ttl_store is None:
            return ImageBuildContainerLifecycleResult(
                status=ImageBuildContainerLifecycleStatus.Skipped,
                operation=ImageBuildContainerLifecycleOperation.StartSession,
                container_id=session.container_id,
                cache_metadata={
                    **metadata,
                    "build_container_ttl_status": ImageBuildContainerLifecycleStatus.Skipped.value,
                },
                reason="image build container TTL store is not configured",
            )
        try:
            refreshed = self.ttl_store.set_build_container_ttl(
                session.container_id,
                session.ttl_seconds,
            )
        except Exception as exc:
            return ImageBuildContainerLifecycleResult(
                status=ImageBuildContainerLifecycleStatus.Error,
                operation=ImageBuildContainerLifecycleOperation.StartSession,
                container_id=session.container_id,
                cache_metadata={
                    **metadata,
                    "build_container_ttl_status": ImageBuildContainerLifecycleStatus.Error.value,
                },
                reason=str(exc),
            )
        status = (
            ImageBuildContainerLifecycleStatus.Complete
            if refreshed
            else ImageBuildContainerLifecycleStatus.Error
        )
        return ImageBuildContainerLifecycleResult(
            status=status,
            operation=ImageBuildContainerLifecycleOperation.StartSession,
            container_id=session.container_id,
            cache_metadata={
                **metadata,
                "build_container_ttl_status": status.value,
            },
            reason="image build container TTL refreshed" if refreshed else "TTL refresh failed",
        )

    def cancel_container(
        self,
        container_id: str,
        *,
        context_cancelled: bool,
        build_succeeded: bool,
        container_connected: bool,
        stopping_ttl_seconds: int,
    ) -> ImageBuildContainerLifecycleResult:
        plan = plan_image_build_cancellation(
            context_cancelled=context_cancelled,
            build_succeeded=build_succeeded,
            container_connected=container_connected,
        )
        if not plan.actions:
            return ImageBuildContainerLifecycleResult(
                status=ImageBuildContainerLifecycleStatus.Skipped,
                operation=ImageBuildContainerLifecycleOperation.CancelSession,
                container_id=container_id,
                reason=plan.reason,
            )
        event_ids: list[str] = []
        for action in plan.actions:
            ok, action_events, reason = self._execute_cancel_action(
                action,
                container_id,
                plan,
                stopping_ttl_seconds=stopping_ttl_seconds,
            )
            event_ids.extend(action_events)
            if not ok:
                return ImageBuildContainerLifecycleResult(
                    status=ImageBuildContainerLifecycleStatus.Error,
                    operation=ImageBuildContainerLifecycleOperation.CancelSession,
                    container_id=container_id,
                    actions=plan.actions,
                    event_ids=event_ids,
                    cache_metadata=_cancel_metadata(plan, event_ids, "error"),
                    reason=reason,
                )
        return ImageBuildContainerLifecycleResult(
            status=ImageBuildContainerLifecycleStatus.Complete,
            operation=ImageBuildContainerLifecycleOperation.CancelSession,
            container_id=container_id,
            actions=plan.actions,
            event_ids=event_ids,
            cache_metadata=_cancel_metadata(plan, event_ids, "complete"),
            reason=plan.reason,
        )

    def _execute_cancel_action(
        self,
        action: ImageBuildLifecycleAction,
        container_id: str,
        plan: ImageBuildCancellationPlan,
        *,
        stopping_ttl_seconds: int,
    ) -> tuple[bool, list[str], str]:
        match action:
            case ImageBuildLifecycleAction.SendStopEvent:
                if self.stop_publisher is None:
                    return False, [], "stop-build event publisher is not configured"
                result = self.stop_publisher.send_stop_build(container_id)
                return result.ok, list(result.event_ids), result.reason
            case ImageBuildLifecycleAction.DeletePendingState:
                if self.state_store is None:
                    return False, [], "container state store is not configured"
                deleted = self.state_store.delete_pending_build_container(container_id)
                return (
                    deleted,
                    [],
                    plan.reason if deleted else "pending container state delete failed",
                )
            case ImageBuildLifecycleAction.MarkStopping:
                if self.state_store is None:
                    return False, [], "container state store is not configured"
                marked = self.state_store.mark_build_container_stopping(
                    container_id,
                    stopping_ttl_seconds,
                )
                return marked, [], plan.reason if marked else "container status update failed"
            case ImageBuildLifecycleAction.KillContainer:
                if self.killer is None:
                    return False, [], "build container killer is not configured"
                killed = self.killer.kill_build_container(container_id)
                return killed, [], plan.reason if killed else "build container kill failed"
            case _:
                return True, [], plan.reason


def image_build_lifecycle_session_metadata(
    session: ImageBuildSessionPlan,
) -> dict[str, str]:
    return {
        "build_container_id": session.container_id,
        "build_container_required": str(session.build_container_required).lower(),
        "build_container_steps": ",".join(step.value for step in session.steps),
        "build_container_ttl_seconds": str(session.ttl_seconds),
        "build_container_keepalive_interval_seconds": str(session.keepalive_interval_seconds),
    }


def _cancel_metadata(
    plan: ImageBuildCancellationPlan,
    event_ids: list[str],
    status: str,
) -> dict[str, str]:
    return {
        "build_container_cancel_status": status,
        "build_container_cancel_actions": ",".join(action.value for action in plan.actions),
        "build_container_stop_event_ids": ",".join(event_ids),
    }
