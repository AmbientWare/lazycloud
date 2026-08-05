from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from compute.agent_control import DEFAULT_PRIVATE_EXECUTOR, agent_machine_worker_id
from compute.projection import PoolConfig, normalize_pool_config
from compute.state import ComputeAgentTokenState, ComputePoolState
from compute.telemetry import agent_machine_connected, agent_telemetry_state
from pydantic import Field
from shared.capacity import CAPACITY_OWNER_ID_PATTERN
from shared.compute_policy import ComputePoolRecord
from shared.contracts import ContractModel
from shared.scheduling import (
    SchedulerWorkerRecord,
    SchedulerWorkerStatus,
    WorkerUnavailableReason,
)
from shared.timestamps import utc_now

AGENT_PROVIDER_NAME = "agent"
DEFAULT_AGENT_WORKER_BUILD_VERSION = "local"


class AgentPoolWorkerAction(StrEnum):
    Existing = "existing"
    Ensured = "ensured"
    Disabled = "disabled"
    Skipped = "skipped"


class AgentPoolConfig(ContractModel):
    workspace_id: str
    pool_name: str
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    gpu_type: str = ""
    worker_build_version: str = DEFAULT_AGENT_WORKER_BUILD_VERSION


class AgentPoolWorkerResult(ContractModel):
    action: AgentPoolWorkerAction
    machine_id: str
    worker_id: str = ""
    reason: str = ""


class AgentPoolReconcileResult(ContractModel):
    workspace_id: str
    pool_name: str
    ensured_worker_ids: list[str] = Field(default_factory=list)
    existing_worker_ids: list[str] = Field(default_factory=list)
    disabled_worker_ids: list[str] = Field(default_factory=list)
    skipped_machine_ids: list[str] = Field(default_factory=list)

    @property
    def touched_count(self) -> int:
        return len(self.ensured_worker_ids) + len(self.disabled_worker_ids)


class AgentMachineRepository(Protocol):
    def list_agent_token_states(
        self,
        workspace_id: str,
        pool_name: str,
    ) -> list[ComputeAgentTokenState]: ...


class AgentWorkerRepository(Protocol):
    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None: ...

    def add_worker(
        self,
        worker: SchedulerWorkerRecord,
        *,
        ttl_seconds: int = 0,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord: ...

    def disable_worker(
        self,
        worker_id: str,
        *,
        reason: WorkerUnavailableReason,
        detail: str = "",
        ttl_seconds: int = 0,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord: ...


@dataclass(slots=True)
class AgentWorkerPoolController:
    config: AgentPoolConfig
    machines: AgentMachineRepository
    workers: AgentWorkerRepository
    expected_executor: str = DEFAULT_PRIVATE_EXECUTOR

    def reconcile(self, *, now: datetime | None = None) -> AgentPoolReconcileResult:
        current_time = now or utc_now()
        result = AgentPoolReconcileResult(
            workspace_id=self.config.workspace_id,
            pool_name=self.config.pool_name,
        )
        for machine in self.machines.list_agent_token_states(
            self.config.workspace_id,
            self.config.pool_name,
        ):
            outcome = self.ensure_machine_worker(machine, now=current_time)
            if outcome.action is AgentPoolWorkerAction.Ensured:
                result.ensured_worker_ids.append(outcome.worker_id)
            elif outcome.action is AgentPoolWorkerAction.Existing:
                result.existing_worker_ids.append(outcome.worker_id)
            elif outcome.action is AgentPoolWorkerAction.Disabled:
                result.disabled_worker_ids.append(outcome.worker_id)
            else:
                result.skipped_machine_ids.append(outcome.machine_id)
        return result

    def ensure_machine_worker(
        self,
        machine: ComputeAgentTokenState,
        *,
        now: datetime | None = None,
    ) -> AgentPoolWorkerResult:
        current_time = now or utc_now()
        worker_id = agent_machine_worker_id(machine.machine_id)
        worker = self.workers.get_worker(worker_id)
        if not self.machine_schedulable(machine, now=current_time):
            if worker is None:
                return AgentPoolWorkerResult(
                    action=AgentPoolWorkerAction.Skipped,
                    machine_id=machine.machine_id,
                    worker_id=worker_id,
                    reason="agent machine is not schedulable",
                )
            if worker.status is SchedulerWorkerStatus.Unavailable:
                return AgentPoolWorkerResult(
                    action=AgentPoolWorkerAction.Existing,
                    machine_id=machine.machine_id,
                    worker_id=worker.worker_id,
                    reason="agent machine worker already disabled",
                )
            disabled = self.workers.disable_worker(
                worker.worker_id,
                reason=WorkerUnavailableReason.AgentDisconnected,
                now=current_time,
            )
            return AgentPoolWorkerResult(
                action=AgentPoolWorkerAction.Disabled,
                machine_id=machine.machine_id,
                worker_id=disabled.worker_id,
                reason="agent machine is not schedulable",
            )
        if worker is not None and worker.status is not SchedulerWorkerStatus.Unavailable:
            return AgentPoolWorkerResult(
                action=AgentPoolWorkerAction.Existing,
                machine_id=machine.machine_id,
                worker_id=worker.worker_id,
                reason="agent machine worker already exists",
            )
        ensured = self.workers.add_worker(
            agent_machine_worker_record(machine, self.config, now=current_time),
            now=current_time,
        )
        return AgentPoolWorkerResult(
            action=AgentPoolWorkerAction.Ensured,
            machine_id=machine.machine_id,
            worker_id=ensured.worker_id,
            reason="agent machine worker ensured",
        )

    def machine_schedulable(
        self,
        machine: ComputeAgentTokenState,
        *,
        now: datetime | None = None,
    ) -> bool:
        return agent_machine_schedulable(
            machine,
            self.config,
            now=now,
            expected_executor=self.expected_executor,
        )


@dataclass(slots=True)
class SchedulerAgentPoolService:
    machines: AgentMachineRepository
    workers: AgentWorkerRepository

    def controller(self, config: AgentPoolConfig) -> AgentWorkerPoolController:
        return AgentWorkerPoolController(config, self.machines, self.workers)

    def reconcile(
        self,
        configs: list[AgentPoolConfig],
        *,
        now: datetime | None = None,
    ) -> list[AgentPoolReconcileResult]:
        return [self.controller(config).reconcile(now=now) for config in configs]


def agent_pool_config_from_pool(pool: ComputePoolRecord) -> AgentPoolConfig | None:
    """Config for a unit that runs agent machines, or None if it runs none.

    The unit's own provider column decides: a unit whose machines are joined or
    locally hosted is driven by the agent pool controller, and one backed by a
    cloud auto-scaling group is not.
    """
    if pool.provider not in AGENT_MACHINE_PROVIDERS:
        return None
    return AgentPoolConfig(
        workspace_id=pool.workspace_id,
        pool_name=pool.name,
        capacity_owner_id=pool.capacity_owner_id,
        gpu_type=pool.worker_gpu_type,
        worker_build_version=DEFAULT_AGENT_WORKER_BUILD_VERSION,
    )


def agent_pool_config_from_compute_state(state: ComputePoolState) -> AgentPoolConfig:
    config = _pool_config_from_metadata(state)
    normalized = normalize_pool_config(config)
    return AgentPoolConfig(
        workspace_id=state.workspace_id,
        pool_name=state.name,
        capacity_owner_id=state.capacity_owner_id,
        gpu_type=(normalized.gpu[0] if normalized and normalized.gpu else ""),
        worker_build_version=str(
            state.metadata.get("worker_build_version") or DEFAULT_AGENT_WORKER_BUILD_VERSION
        ),
    )


def agent_machine_worker_record(
    machine: ComputeAgentTokenState,
    config: AgentPoolConfig,
    *,
    now: datetime | None = None,
) -> SchedulerWorkerRecord:
    current_time = now or utc_now()
    cpu_millicores = machine.cpu_millicores or machine.cpu_count * 1000
    gpu_types = _machine_gpu_types(machine, config)
    return SchedulerWorkerRecord(
        worker_id=agent_machine_worker_id(machine.machine_id),
        pool_name=config.pool_name,
        capacity_owner_id=config.capacity_owner_id,
        machine_id=machine.machine_id,
        status=SchedulerWorkerStatus.Pending,
        gpu_type=gpu_types[0] if gpu_types else "",
        runtime_class="runc",
        runtime_classes=["runc"],
        private_worker=True,
        requires_pool_selector=True,
        free_cpu_millicores=cpu_millicores,
        free_memory_mib=machine.memory_mb,
        free_gpu_count=machine.gpu_count,
        total_cpu_millicores=cpu_millicores,
        total_memory_mib=machine.memory_mb,
        total_gpu_count=machine.gpu_count,
        created_at=current_time,
        updated_at=current_time,
    )


def agent_machine_schedulable(
    machine: ComputeAgentTokenState,
    config: AgentPoolConfig,
    *,
    now: datetime | None = None,
    expected_executor: str = DEFAULT_PRIVATE_EXECUTOR,
) -> bool:
    return (
        machine.workspace_id == config.workspace_id
        and machine.pool_name == config.pool_name
        and machine.executor == expected_executor
        and agent_machine_connected(agent_telemetry_state(machine), now=now)
    )


def _machine_gpu_types(machine: ComputeAgentTokenState, config: AgentPoolConfig) -> list[str]:
    values: list[str] = []
    if config.gpu_type:
        values.append(config.gpu_type)
    values.extend(gpu for gpu in machine.gpus if gpu)
    return list(dict.fromkeys(values))


def _pool_config_from_metadata(state: ComputePoolState) -> PoolConfig:
    raw_config = state.metadata.get("config")
    if isinstance(raw_config, dict):
        return PoolConfig.model_validate(raw_config)
    return PoolConfig(name=state.name)
