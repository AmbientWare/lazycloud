from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from coordination.event_bus import (
    EventBusEvent,
    EventBusEventType,
    RedisEventBus,
    event_id_for_event,
)
from coordination.redis_client import RedisClient, redis_text
from database.context import ServiceContext
from database.repositories.orchestration import MachineRepository, WorkerRepository
from scheduler.state import RedisSchedulerContainerRepository
from shared.compute_fleet import ResourceStatus
from shared.container_requests import ContainerShutdownTarget, StopContainerReason
from shared.errors import UpstreamUnavailableError
from shared.scheduling import SchedulerContainerStatus, SchedulerWorkerRecord


class SchedulerWorkerDirectory(Protocol):
    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None: ...


class DurableWorkerAbsence(Protocol):
    def is_absent(self, worker_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class DatabaseDurableWorkerAbsence:
    context: ServiceContext
    scheduler_workers: SchedulerWorkerDirectory

    def is_absent(self, worker_id: str) -> bool:
        if self.scheduler_workers.get_worker(worker_id) is not None:
            return False
        with self.context.database.session() as session:
            worker = WorkerRepository(session).get_across_workspaces(worker_id)
            if worker is None or worker.status is not ResourceStatus.Deleted:
                return False
            if worker.machine_id is None:
                return True
            machine = MachineRepository(session).get_across_workspaces(worker.machine_id)
            return machine is not None and machine.status is ResourceStatus.Deleted


@dataclass(frozen=True, slots=True)
class ContainerShutdownDispatch:
    container_id: str
    event_id: str
    worker_id: str


@dataclass(frozen=True, slots=True)
class ContainerShutdownService:
    containers: RedisSchedulerContainerRepository
    events: RedisEventBus
    redis: RedisClient
    durable_worker_absence: DurableWorkerAbsence | None = None
    poll_interval_seconds: float = 0.05

    def confirm(
        self,
        targets: list[ContainerShutdownTarget],
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        normalized_by_id: dict[str, ContainerShutdownTarget] = {}
        for target in targets:
            current = normalized_by_id.get(target.container_id)
            if current is None or (not current.worker_id and target.worker_id):
                normalized_by_id[target.container_id] = target
        normalized = list(normalized_by_id.values())
        dispatches = self._dispatch(normalized)
        self._wait(
            normalized,
            dispatches=dispatches,
            timeout_seconds=timeout_seconds,
        )

    def _dispatch(
        self,
        targets: list[ContainerShutdownTarget],
    ) -> tuple[ContainerShutdownDispatch, ...]:
        terminal = {SchedulerContainerStatus.Complete, SchedulerContainerStatus.Failed}
        dispatches: list[ContainerShutdownDispatch] = []
        for target in targets:
            state = self.containers.get_container_state(target.container_id)
            if state is not None and state.status in terminal:
                for worker_id in {target.worker_id, state.worker_id} - {""}:
                    event = _stop_container_event(target.container_id, worker_id=worker_id)
                    self._cleanup_dispatch(
                        ContainerShutdownDispatch(
                            container_id=target.container_id,
                            event_id=event_id_for_event(event),
                            worker_id=worker_id,
                        )
                    )
                continue
            worker_id = target.worker_id or (state.worker_id if state is not None else "")
            if not worker_id:
                continue
            if self._worker_is_durably_absent(worker_id):
                self._finalize_absent_worker(target.container_id, worker_id)
                continue
            event = _stop_container_event(target.container_id, worker_id=worker_id)
            sent = self.events.send(event)
            pending_key = self._pending_event_key(worker_id)
            self.redis.set_add(pending_key, sent.event_id)
            self.redis.expire(pending_key, 300)
            dispatches.append(
                ContainerShutdownDispatch(
                    container_id=target.container_id,
                    event_id=sent.event_id,
                    worker_id=worker_id,
                )
            )
        return tuple(dispatches)

    def _wait(
        self,
        targets: list[ContainerShutdownTarget],
        *,
        dispatches: tuple[ContainerShutdownDispatch, ...],
        timeout_seconds: float,
    ) -> None:
        remaining = {target.container_id: target for target in targets}
        dispatch_by_container = {dispatch.container_id: dispatch for dispatch in dispatches}
        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        terminal = {SchedulerContainerStatus.Complete, SchedulerContainerStatus.Failed}
        while remaining and time.monotonic() < deadline:
            states = {
                container_id: self.containers.get_container_state(container_id)
                for container_id in remaining
            }
            for container_id in tuple(remaining):
                state = states[container_id]
                if state is not None and state.status in terminal:
                    remaining.pop(container_id)
                    continue
                dispatch = dispatch_by_container.get(container_id)
                if dispatch is None and self.containers.is_container_cancelled(container_id):
                    remaining.pop(container_id)
                    continue
                if dispatch is not None and self._worker_is_durably_absent(dispatch.worker_id):
                    self._finalize_absent_worker(container_id, dispatch.worker_id)
                    remaining.pop(container_id)
                    continue
                if state is not None or dispatch is None:
                    continue
                acknowledged = {
                    redis_text(worker_id)
                    for worker_id in self.redis.set_members(self._event_ack_key(dispatch.event_id))
                }
                if dispatch.worker_id in acknowledged:
                    remaining.pop(container_id)
            if remaining:
                time.sleep(self.poll_interval_seconds)
        if remaining:
            missing_workers = sorted(
                {
                    dispatch.worker_id
                    for container_id, dispatch in dispatch_by_container.items()
                    if container_id in remaining
                }
            )
            details: list[str] = []
            if remaining:
                details.append("containers=" + ",".join(sorted(remaining)))
            if missing_workers:
                details.append("workers=" + ",".join(missing_workers))
            raise UpstreamUnavailableError(
                "container shutdown was not confirmed by workers: " + "; ".join(details)
            )
        for dispatch in dispatches:
            self._cleanup_dispatch(dispatch)

    def _cleanup_dispatch(self, dispatch: ContainerShutdownDispatch) -> None:
        self.events.delete(dispatch.event_id)
        self.redis.delete(self._event_ack_key(dispatch.event_id))
        self.redis.set_remove(
            self._pending_event_key(dispatch.worker_id),
            dispatch.event_id,
        )

    def _dispatch_for(self, container_id: str, worker_id: str) -> ContainerShutdownDispatch:
        event = _stop_container_event(container_id, worker_id=worker_id)
        return ContainerShutdownDispatch(
            container_id=container_id,
            event_id=event_id_for_event(event),
            worker_id=worker_id,
        )

    def _finalize_absent_worker(self, container_id: str, worker_id: str) -> None:
        self.containers.delete_container_state(container_id)
        self._cleanup_dispatch(self._dispatch_for(container_id, worker_id))

    def _worker_is_durably_absent(self, worker_id: str) -> bool:
        return self.durable_worker_absence is not None and self.durable_worker_absence.is_absent(
            worker_id
        )

    def _pending_event_key(self, worker_id: str) -> str:
        return self.redis.key("worker-events", "pending", worker_id)

    def _event_ack_key(self, event_id: str) -> str:
        return self.redis.key("worker-events", "ack", event_id)


def _stop_container_event(container_id: str, *, worker_id: str) -> EventBusEvent:
    return EventBusEvent(
        type=EventBusEventType.StopContainer,
        args={
            "container_id": container_id,
            "force": False,
            "reason": StopContainerReason.User.value,
            "worker_id": worker_id,
        },
        retries=3,
    )


__all__ = [
    "ContainerShutdownDispatch",
    "ContainerShutdownService",
    "DatabaseDurableWorkerAbsence",
    "DurableWorkerAbsence",
]
