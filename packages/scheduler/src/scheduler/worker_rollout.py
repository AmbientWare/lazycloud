from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Protocol

from database.repositories.apps import StubRepository
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.endpoint_dispatch import EndpointDispatchRepository
from database.repositories.execution import TaskRepository
from database.repositories.orchestration import ContainerRepository
from shared.container_requests import StopContainerReason
from shared.containers import LIVE_CONTAINER_STATUSES, ContainerRecord
from shared.deployments import StubKind
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerStatus,
)
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner

from database import DatabaseClient

WORKER_UPDATE_DRAIN_GRACE = timedelta(seconds=60)


def worker_rollout_allowance(
    worker: SchedulerWorkerRecord,
    fleet: list[SchedulerWorkerRecord],
    *,
    now: datetime,
) -> int:
    platform = worker.billing_owner is UsageBillingOwner.PlatformFleet
    serving = [
        candidate
        for candidate in fleet
        if (
            candidate.billing_owner is UsageBillingOwner.PlatformFleet
            and bool(candidate.total_gpu_count) == bool(worker.total_gpu_count)
            if platform
            else candidate.capacity_owner_id == worker.capacity_owner_id
        )
    ]
    limit = 1 if platform else max(1, ceil(len(serving) * 0.1))
    if worker.status is SchedulerWorkerStatus.Draining:
        return limit
    if worker.request_intake_status(at=now) is not SchedulerWorkerStatus.Available:
        return limit
    available_after = sum(
        candidate.worker_id != worker.worker_id
        and candidate.request_intake_status(at=now) is SchedulerWorkerStatus.Available
        for candidate in serving
    )
    required = max(int(platform), len(serving) - limit)
    return limit if available_after >= required else 0


class WorkerRolloutContainers(Protocol):
    def list_by_worker(self, worker_id: str) -> list[SchedulerContainerState]: ...


class WorkerRolloutStopper(Protocol):
    def stop(self, container_id: str, *, reason: StopContainerReason) -> ContainerRecord: ...


class WorkerRolloutRequests(Protocol):
    def has_recoverable_container_request(self, container_id: str, *, worker_id: str) -> bool: ...


@dataclass(slots=True)
class WorkerWorkloadDrainService:
    database: DatabaseClient
    containers: WorkerRolloutContainers

    def prepare(
        self, worker_id: str, *, now: datetime, close_admission: bool = False
    ) -> list[tuple[ContainerRecord, StubKind]]:
        candidates: list[tuple[ContainerRecord, StubKind]] = []
        with self.database.session() as session:
            containers = ContainerRepository(session)
            rollouts = ContainerRolloutRepository(session)
            floors: dict[str, int] = {}
            for state in self.containers.list_by_worker(worker_id):
                container = containers.get_across_workspaces(state.container_id)
                if (
                    container is None
                    or container.status not in LIVE_CONTAINER_STATUSES
                    or not container.stub_id
                ):
                    continue
                stub = StubRepository(session).get_across_workspaces(container.stub_id)
                if stub is None or stub.kind not in {
                    StubKind.Function,
                    StubKind.Endpoint,
                    StubKind.Asgi,
                }:
                    continue
                if stub.id not in floors:
                    floors[stub.id] = max(
                        containers.count_live_for_stub(stub.id),
                        rollouts.serving_floor(stub.id),
                        1,
                    )
                candidates.append((container, stub.kind))
                rollouts.prepare(container, serving_floor=floors[stub.id], now=now)
                if close_admission:
                    rollouts.close_admission(container.id, now=now)
        return candidates


@dataclass(slots=True)
class WorkerWorkloadRolloutService:
    database: DatabaseClient
    containers: WorkerRolloutContainers
    stopper: WorkerRolloutStopper
    requests: WorkerRolloutRequests

    def reconcile(self, worker_id: str, *, now: datetime | None = None) -> None:
        current_time = now or utc_now()
        stop: list[tuple[str, StopContainerReason]] = []
        with self.database.session() as session:
            containers = ContainerRepository(session)
            rollouts = ContainerRolloutRepository(session)
            for state in self.containers.list_by_worker(worker_id):
                if state.status not in {
                    SchedulerContainerStatus.Pending,
                    SchedulerContainerStatus.Running,
                }:
                    continue
                if self.requests.has_recoverable_container_request(
                    state.container_id, worker_id=worker_id
                ):
                    continue
                container = containers.get_across_workspaces(state.container_id)
                if (
                    container is None
                    or container.status not in LIVE_CONTAINER_STATUSES
                    or not container.stub_id
                ):
                    continue
                stub = StubRepository(session).get_across_workspaces(container.stub_id)
                if stub is None:
                    continue
                rollouts.prepare(container, serving_floor=0, now=current_time)
                if not rollouts.close_admission(container.id, now=current_time):
                    continue
                closed_at = rollouts.admission_closed_at(container.id)
                can_preempt = (
                    state.preemptible
                    and closed_at is not None
                    and current_time - closed_at >= WORKER_UPDATE_DRAIN_GRACE
                )
                has_work = bool(
                    TaskRepository(session).containers_with_inflight_work([container.id])
                    or EndpointDispatchRepository(session)
                    .inflight_counts(container.stub_id, at=current_time)
                    .get(container.id, 0)
                )
                if stub.kind not in {StubKind.Function, StubKind.Endpoint, StubKind.Asgi}:
                    has_work = True
                if has_work and not can_preempt:
                    continue
                stop.append(
                    (
                        container.id,
                        StopContainerReason.Preempted
                        if has_work
                        else StopContainerReason.Scheduler,
                    )
                )
        for container_id, reason in stop:
            self.stopper.stop(container_id, reason=reason)
