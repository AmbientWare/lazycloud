from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from compute.state import RedisComputeStateRepository
from shared.capacity import CapacityOwnerKind

from scheduler.agent_pool import (
    AgentPoolConfig,
    agent_pool_config_from_compute_state,
    agent_pool_config_from_pool,
)
from scheduler.capacity_reservations import (
    CapacityAcquisitionController,
    CapacityWorkerRepository,
    ComputePoolCapacityController,
)
from scheduler.pool_drain import (
    WorkerPoolDrainContainerRepository,
    WorkerPoolDrainController,
    WorkerPoolDrainWorkerRepository,
    managed_compute_drain_controllers,
)
from scheduler.services import SchedulerServices


class SchedulerCapacityWorkerRepository(
    WorkerPoolDrainWorkerRepository,
    CapacityWorkerRepository,
    Protocol,
):
    pass


@dataclass(frozen=True, slots=True)
class SchedulerCapacityControllerProvider:
    services: SchedulerServices
    compute_states: RedisComputeStateRepository
    workers: SchedulerCapacityWorkerRepository
    containers: WorkerPoolDrainContainerRepository

    def agent_pool_configs(self) -> list[AgentPoolConfig]:
        configs: dict[tuple[str, str], AgentPoolConfig] = {}
        for workspace_id, pool in self.services.compute.list_pools_across_workspaces():
            config = agent_pool_config_from_pool(pool, workspace_id=workspace_id)
            if config is not None:
                configs[(config.workspace_id, config.pool_name)] = config
        for state in self.compute_states.list_all_pool_states():
            config = agent_pool_config_from_compute_state(state)
            configs[(config.workspace_id, config.pool_name)] = config
        return [configs[key] for key in sorted(configs, key=lambda item: (item[0], item[1]))]

    def capacity_acquisition_controllers(self) -> list[CapacityAcquisitionController]:
        controllers: list[CapacityAcquisitionController] = []
        for workspace_id, pool in self.services.compute.list_pools_across_workspaces():
            if pool.capacity_owner_kind in {
                CapacityOwnerKind.ManagedPool,
                CapacityOwnerKind.PooledProvider,
            }:
                controllers.append(
                    ComputePoolCapacityController(
                        workspace_id,
                        pool,
                        self.services.compute,
                        self.workers,
                    )
                )
        controllers.sort(key=lambda item: item.capacity_owner_id)
        return controllers

    def worker_pool_drain_controllers(self) -> list[WorkerPoolDrainController]:
        controllers: list[WorkerPoolDrainController] = []
        controllers.extend(
            managed_compute_drain_controllers(
                self.services.compute,
                self.compute_states,
                self.workers,
                self.containers,
            )
        )
        return controllers
