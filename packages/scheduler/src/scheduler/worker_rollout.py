from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from database.repositories.apps import StubRepository
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.endpoint_dispatch import EndpointDispatchRepository
from database.repositories.execution import TaskRepository
from database.repositories.orchestration import ContainerRepository
from shared.container_requests import StopContainerReason
from shared.containers import LIVE_CONTAINER_STATUSES, ContainerRecord
from shared.deployments import StubKind
from shared.scheduling import SchedulerContainerState, SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now

from database import DatabaseClient


class WorkerRolloutContainers(Protocol):
    def list_by_worker(self, worker_id: str) -> list[SchedulerContainerState]: ...


class WorkerRolloutStopper(Protocol):
    def stop(self, container_id: str, *, reason: StopContainerReason) -> ContainerRecord: ...


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
    endpoint_readiness: Callable[[str, list[str]], set[str]]

    def reconcile(self, worker_id: str, *, workers: list[SchedulerWorkerRecord]) -> None:
        now = utc_now()
        available = {
            worker.worker_id
            for worker in workers
            if worker.status is SchedulerWorkerStatus.Available
        }
        candidates = WorkerWorkloadDrainService(self.database, self.containers).prepare(
            worker_id, now=now
        )
        kinds = {container.stub_id: kind for container, kind in candidates if container.stub_id}

        ready: dict[str, set[str]] = {}
        endpoint_candidates: dict[str, list[str]] = {}
        with self.database.session() as session:
            rollouts = ContainerRolloutRepository(session)
            for stub_id, kind in kinds.items():
                replacements = [
                    item.id
                    for item in rollouts.serving_containers(stub_id)
                    if (item.runtime_worker_id or item.worker_id) in available
                ]
                if kind is StubKind.Function:
                    ready[stub_id] = rollouts.ready_container_ids(replacements)
                else:
                    endpoint_candidates[stub_id] = replacements
        for stub_id, replacements in endpoint_candidates.items():
            ready[stub_id] = self.endpoint_readiness(stub_id, replacements)

        stop: list[str] = []
        with self.database.session() as session:
            rollouts = ContainerRolloutRepository(session)
            for container, _kind in candidates:
                if not container.stub_id:
                    continue
                if len(ready[container.stub_id]) < rollouts.serving_floor(container.stub_id):
                    continue
                if not rollouts.close_admission(container.id, now=now):
                    continue
                if TaskRepository(session).containers_with_inflight_work([container.id]):
                    continue
                if (
                    EndpointDispatchRepository(session)
                    .inflight_counts(container.stub_id, at=now)
                    .get(container.id, 0)
                ):
                    continue
                stop.append(container.id)
        for container_id in stop:
            self.stopper.stop(container_id, reason=StopContainerReason.Scheduler)
