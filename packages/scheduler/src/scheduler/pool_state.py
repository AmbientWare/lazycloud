from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from compute.agent_control import agent_machine_worker_id
from compute.state import ComputeAgentTokenState
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerWorkerRecord,
    SchedulerWorkerStatus,
)

from scheduler.agent_pool import (
    AgentMachineRepository,
    AgentPoolConfig,
    agent_machine_schedulable,
)
from scheduler.fleet import (
    SchedulerContainerSnapshot,
    SchedulerMachineSnapshot,
    SchedulerMachineStatus,
    SchedulerWorkerSnapshot,
    WorkerPoolStateSnapshot,
    plan_pool_state,
)
from scheduler.tools import WorkerPoolCapacity


class SchedulerPoolWorkerRepository(Protocol):
    def list_workers(self) -> list[SchedulerWorkerRecord]: ...

    def list_workers_for_capacity_owner(
        self,
        capacity_owner_id: str,
    ) -> list[SchedulerWorkerRecord]: ...

    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None: ...


class SchedulerPoolContainerRepository(Protocol):
    def list_by_worker(self, worker_id: str) -> list[SchedulerContainerState]: ...


class SchedulerPoolStateRepository(Protocol):
    def set_state(
        self,
        capacity_owner_id: str,
        state: WorkerPoolStateSnapshot,
    ) -> WorkerPoolStateSnapshot: ...


@dataclass(slots=True)
class SchedulerPoolStateService:
    workers: SchedulerPoolWorkerRepository
    containers: SchedulerPoolContainerRepository
    pool_states: SchedulerPoolStateRepository
    agent_machines: AgentMachineRepository | None = None

    def refresh(
        self,
        *,
        agent_pool_configs: list[AgentPoolConfig] | None = None,
        now: datetime | None = None,
    ) -> dict[str, WorkerPoolStateSnapshot]:
        configs_by_owner: dict[str, AgentPoolConfig] = {}
        pool_names_by_owner: dict[str, str] = {}
        for config in agent_pool_configs or []:
            self._register_capacity_owner(
                pool_names_by_owner,
                capacity_owner_id=config.capacity_owner_id,
                pool=config.pool,
            )
            existing = configs_by_owner.setdefault(config.capacity_owner_id, config)
            if existing != config:
                raise RuntimeError(
                    f"capacity owner {config.capacity_owner_id!r} has multiple agent pool configs"
                )
        for worker in self.workers.list_workers():
            self._register_capacity_owner(
                pool_names_by_owner,
                capacity_owner_id=worker.capacity_owner_id,
                pool=worker.pool,
            )
        states: dict[str, WorkerPoolStateSnapshot] = {}
        for capacity_owner_id in sorted(pool_names_by_owner):
            state = self.refresh_pool(
                capacity_owner_id,
                pool=pool_names_by_owner[capacity_owner_id],
                agent_pool_config=configs_by_owner.get(capacity_owner_id),
                now=now,
            )
            states[capacity_owner_id] = state
        return states

    def refresh_pool(
        self,
        capacity_owner_id: str,
        *,
        pool: str,
        agent_pool_config: AgentPoolConfig | None = None,
        now: datetime | None = None,
    ) -> WorkerPoolStateSnapshot:
        self._require_capacity_owner(capacity_owner_id)
        workers = self.workers.list_workers_for_capacity_owner(capacity_owner_id)
        if any(worker.pool != pool for worker in workers):
            raise RuntimeError(
                f"capacity owner {capacity_owner_id!r} contains multiple pool display names"
            )
        if agent_pool_config is not None and (
            agent_pool_config.capacity_owner_id != capacity_owner_id
            or agent_pool_config.pool != pool
        ):
            raise RuntimeError(
                f"agent pool config does not match capacity owner {capacity_owner_id!r}"
            )
        containers_by_worker = {
            worker.worker_id: self.containers.list_by_worker(worker.worker_id) for worker in workers
        }
        state = plan_pool_state(
            workers=[
                SchedulerWorkerSnapshot(
                    worker_id=worker.worker_id,
                    status=worker.status,
                    pool=worker.pool,
                    active_containers=[
                        container.container_id
                        for container in containers_by_worker[worker.worker_id]
                    ],
                )
                for worker in workers
            ],
            containers_by_worker={
                worker.worker_id: [
                    SchedulerContainerSnapshot(
                        container_id=container.container_id,
                        status=container.status,
                        scheduled_at=container.scheduled_at,
                        started_at=container.started_at,
                    )
                    for container in containers_by_worker[worker.worker_id]
                ]
                for worker in workers
            },
            machines=self._agent_machine_snapshots(agent_pool_config, now=now),
            free_capacity=_free_capacity(workers),
            now=now,
        ).model_copy(
            update={
                "capacity_owner_id": capacity_owner_id,
                "pool": pool,
            }
        )
        return self.pool_states.set_state(capacity_owner_id, state)

    @classmethod
    def _register_capacity_owner(
        cls,
        pool_names_by_owner: dict[str, str],
        *,
        capacity_owner_id: str,
        pool: str,
    ) -> None:
        cls._require_capacity_owner(capacity_owner_id)
        existing_pool_name = pool_names_by_owner.setdefault(capacity_owner_id, pool)
        if existing_pool_name != pool:
            raise RuntimeError(
                f"capacity owner {capacity_owner_id!r} contains multiple pool display names"
            )

    @staticmethod
    def _require_capacity_owner(capacity_owner_id: str) -> None:
        if not capacity_owner_id:
            raise RuntimeError("scheduler pool state requires a capacity owner")

    def _agent_machine_snapshots(
        self,
        config: AgentPoolConfig | None,
        *,
        now: datetime | None,
    ) -> list[SchedulerMachineSnapshot]:
        if config is None or self.agent_machines is None:
            return []
        return [
            SchedulerMachineSnapshot(
                machine_id=machine.machine_id,
                status=self._agent_machine_status(machine, config, now=now),
            )
            for machine in self.agent_machines.list_agent_token_states(
                config.workspace_id,
                config.pool,
            )
        ]

    def _agent_machine_status(
        self,
        machine: ComputeAgentTokenState,
        config: AgentPoolConfig,
        *,
        now: datetime | None,
    ) -> SchedulerMachineStatus:
        if not agent_machine_schedulable(machine, config, now=now):
            return SchedulerMachineStatus.Registered
        worker = self.workers.get_worker(agent_machine_worker_id(machine.machine_id))
        if worker is not None and worker.status is SchedulerWorkerStatus.Available:
            return SchedulerMachineStatus.Ready
        return SchedulerMachineStatus.Pending


def _free_capacity(workers: list[SchedulerWorkerRecord]) -> WorkerPoolCapacity:
    schedulable = [
        worker
        for worker in workers
        if worker.status in {SchedulerWorkerStatus.Available, SchedulerWorkerStatus.Pending}
    ]
    return WorkerPoolCapacity(
        free_cpu=sum(worker.free_cpu_millicores for worker in schedulable) / 1000,
        free_memory_mib=sum(worker.free_memory_mib for worker in schedulable),
        free_gpu=sum(worker.free_gpu_count for worker in schedulable),
    )
