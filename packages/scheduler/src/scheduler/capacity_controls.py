from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from compute.state import RedisComputeStateRepository
from shared.capacity import CapacityOwnerKind, capacity_owner_for_provider

from scheduler.agent_pool import (
    AgentPoolConfig,
    agent_pool_config_from_compute_state,
    agent_pool_config_from_pool,
)
from scheduler.capacity_reservations import (
    CapacityAcquisitionController,
    CapacityWorkerRepository,
    ComputePoolCapacityController,
    PendingCapacityOwner,
    StaticWorkerPoolCapacityController,
)
from scheduler.pool_drain import (
    WorkerPoolDrainContainerRepository,
    WorkerPoolDrainController,
    WorkerPoolDrainWorkerRepository,
    managed_compute_drain_controllers,
    static_worker_pool_drain_controllers,
)
from scheduler.pool_sizing import (
    WorkerPoolReplicaScaler,
    WorkerPoolReplicaStateStore,
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
    replica_states: WorkerPoolReplicaStateStore
    workers: SchedulerCapacityWorkerRepository
    containers: WorkerPoolDrainContainerRepository
    worker_pool_replicas: WorkerPoolReplicaScaler | None

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
            if pool.provider == "kubernetes":
                if self.worker_pool_replicas is not None:
                    controllers.append(
                        StaticWorkerPoolCapacityController(
                            pool,
                            self.worker_pool_replicas,
                            self.workers,
                            self.services.compute,
                        )
                    )
            elif pool.capacity_owner_kind in {
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

    def pending_capacity_owners(self) -> list[PendingCapacityOwner]:
        owners: dict[str, PendingCapacityOwner] = {}
        for workspace_id, pool in self.services.compute.list_pools_across_workspaces():
            owners[pool.capacity_owner_id] = PendingCapacityOwner(
                capacity_owner_id=pool.capacity_owner_id,
                owner_kind=pool.capacity_owner_kind,
                pool_name=pool.name,
                workspace_id=(
                    ""
                    if pool.capacity_owner_kind is CapacityOwnerKind.GlobalKubernetesDeployment
                    else workspace_id
                ),
                registration_timeout_seconds=pool.registration_timeout_seconds,
            )
        for state in self.compute_states.list_all_pool_states():
            if state.capacity_owner_id in owners:
                continue
            owner_kind, _source = capacity_owner_for_provider(state.provider)
            owners[state.capacity_owner_id] = PendingCapacityOwner(
                capacity_owner_id=state.capacity_owner_id,
                owner_kind=owner_kind,
                pool_name=state.name,
                workspace_id=state.workspace_id,
            )
        return [owners[key] for key in sorted(owners)]

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
        if self.worker_pool_replicas is not None:
            controllers.extend(
                static_worker_pool_drain_controllers(
                    [pool for _, pool in self.services.compute.list_pools_across_workspaces()],
                    self.workers,
                    self.containers,
                    self.worker_pool_replicas,
                    self.replica_states,
                    self.services.compute,
                )
            )
        return controllers
