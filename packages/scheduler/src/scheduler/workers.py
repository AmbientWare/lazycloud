from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import Field
from shared.compute_policy import MachinePool
from shared.contracts import ContractModel
from shared.errors import ConflictError, NotFoundError
from shared.routing import AgentBackendRoute
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    WorkerRemovalResult,
    WorkerUnavailableReason,
)


class SchedulerWorkerAdminRepository(Protocol):
    def resume_worker_registration(
        self, worker: SchedulerWorkerRecord
    ) -> SchedulerWorkerRecord: ...

    def renew_worker_update(
        self, worker: SchedulerWorkerRecord, *, expires_at: datetime
    ) -> SchedulerWorkerRecord: ...

    def list_workers(self) -> list[SchedulerWorkerRecord]: ...

    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None: ...

    def reconcile_worker_capacity(self, worker_id: str) -> SchedulerWorkerRecord: ...

    def has_recoverable_container_request(
        self,
        container_id: str,
        *,
        worker_id: str = "",
    ) -> bool: ...

    def claim_worker_rollout_slot(
        self,
        capacity_owner_id: str,
        worker_id: str,
        target_revision: str,
        *,
        max_unavailable: int,
        now: datetime,
    ) -> bool: ...

    def release_worker_rollout_slot(
        self,
        capacity_owner_id: str,
        worker_id: str,
        target_revision: str,
    ) -> bool: ...

    def toggle_worker_available(self, worker_id: str) -> SchedulerWorkerRecord: ...

    def disable_worker(
        self,
        worker_id: str,
        *,
        reason: WorkerUnavailableReason,
        detail: str = "",
    ) -> SchedulerWorkerRecord: ...

    def remove_worker(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> WorkerRemovalResult: ...


class SchedulerWorkerContainerRepository(Protocol):
    def list_by_worker(self, worker_id: str) -> list[SchedulerContainerState]: ...

    def update_backend_route(
        self,
        route: AgentBackendRoute,
    ) -> AgentBackendRoute | None: ...


class SchedulerWorkerContainerStopper(Protocol):
    def stop_container(self, container_id: str) -> None: ...


class SchedulerWorkerContainerView(ContractModel):
    container_id: str
    workspace_id: str = ""
    stub_id: str = ""
    status: str = ""
    scheduled_at: datetime
    started_at: datetime | None = None


class SchedulerWorkerView(ContractModel):
    id: str
    status: str
    pool: MachinePool
    machine_id: str = ""
    gpu: str = ""
    runtime: str = ""
    total_cpu: int = 0
    total_memory: int = 0
    total_gpu_count: int = 0
    free_cpu: int = 0
    free_memory: int = 0
    free_gpu_count: int = 0
    resource_version: int = 0
    requires_pool_selector: bool = False
    preemptible: bool = False
    created_at: datetime
    updated_at: datetime
    active_containers: list[SchedulerWorkerContainerView] = Field(default_factory=list)


class SchedulerWorkerDrainResult(ContractModel):
    worker: SchedulerWorkerView
    stopped_container_ids: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class SchedulerWorkerAdminService:
    workers: SchedulerWorkerAdminRepository
    containers: SchedulerWorkerContainerRepository
    stop_container: Callable[[str], None] | SchedulerWorkerContainerStopper | None = None

    def list_workers(self, *, pool: str | None = None) -> list[SchedulerWorkerView]:
        workers = self.workers.list_workers()
        if pool:
            workers = [worker for worker in workers if worker.pool == pool]
        views = [self._worker_view(worker) for worker in workers]
        views.sort(key=lambda item: (item.pool, item.status, item.machine_id, item.id))
        return views

    def get_worker(self, worker_id: str) -> SchedulerWorkerView:
        worker = self.workers.get_worker(worker_id)
        if worker is None:
            msg = f"worker not found: {worker_id}"
            raise NotFoundError(msg)
        return self._worker_view(worker)

    def cordon_worker(self, worker_id: str) -> SchedulerWorkerView:
        self.get_worker(worker_id)
        return self._worker_view(
            self.workers.disable_worker(worker_id, reason=WorkerUnavailableReason.OperatorCordon)
        )

    def uncordon_worker(self, worker_id: str) -> SchedulerWorkerView:
        self.get_worker(worker_id)
        return self._worker_view(self.workers.toggle_worker_available(worker_id))

    def delete_worker(self, worker_id: str, *, now: datetime | None = None) -> WorkerRemovalResult:
        self.get_worker(worker_id)
        return self.workers.remove_worker(worker_id, now=now)

    def drain_worker(self, worker_id: str) -> SchedulerWorkerDrainResult:
        self.get_worker(worker_id)
        disabled = self.workers.disable_worker(worker_id, reason=WorkerUnavailableReason.Draining)
        active_container_ids = [
            container.container_id for container in _active_containers(self.containers, worker_id)
        ]
        stopped: list[str] = []
        errors: list[str] = []
        for container_id in active_container_ids:
            try:
                self._stop_container(container_id)
            except Exception as exc:  # pragma: no cover - defensive service boundary
                errors.append(f"{container_id}: {type(exc).__name__}: {exc}")
                continue
            stopped.append(container_id)
        if errors:
            raise ConflictError("; ".join(errors))
        return SchedulerWorkerDrainResult(
            worker=self._worker_view(disabled),
            stopped_container_ids=stopped,
        )

    def _worker_view(self, worker: SchedulerWorkerRecord) -> SchedulerWorkerView:
        return SchedulerWorkerView(
            id=worker.worker_id,
            status=worker.status.value,
            pool=worker.pool,
            machine_id=worker.machine_id,
            gpu=worker.gpu_type,
            runtime=worker.runtime_class,
            total_cpu=worker.total_cpu_millicores,
            total_memory=worker.total_memory_mib,
            total_gpu_count=worker.total_gpu_count,
            free_cpu=worker.free_cpu_millicores,
            free_memory=worker.free_memory_mib,
            free_gpu_count=worker.free_gpu_count,
            resource_version=worker.resource_version,
            requires_pool_selector=worker.requires_pool_selector,
            preemptible=worker.preemptible,
            created_at=worker.created_at,
            updated_at=worker.updated_at,
            active_containers=[
                _container_view(container)
                for container in _active_containers(self.containers, worker.worker_id)
            ],
        )

    def _stop_container(self, container_id: str) -> None:
        stopper = self.stop_container
        if stopper is None:
            msg = "container stopper is not configured"
            raise RuntimeError(msg)
        if callable(stopper):
            stopper(container_id)
            return
        stopper.stop_container(container_id)


def _active_containers(
    repository: SchedulerWorkerContainerRepository,
    worker_id: str,
) -> list[SchedulerContainerState]:
    return [
        container
        for container in repository.list_by_worker(worker_id)
        if container.status
        in {
            SchedulerContainerStatus.Pending,
            SchedulerContainerStatus.Running,
        }
    ]


def _container_view(container: SchedulerContainerState) -> SchedulerWorkerContainerView:
    return SchedulerWorkerContainerView(
        container_id=container.container_id,
        workspace_id=container.workspace_id,
        stub_id=container.stub_id,
        status=container.status.value,
        scheduled_at=container.scheduled_at,
        started_at=container.started_at,
    )
