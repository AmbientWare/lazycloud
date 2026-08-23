from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from compute.agent_control import DEFAULT_PRIVATE_EXECUTOR, agent_machine_worker_id
from compute.projection import PoolConfig, normalize_unit_config
from compute.state import ComputeAgentTokenState, ComputeUnitState
from compute.telemetry import agent_machine_connected, agent_telemetry_state
from pydantic import Field
from shared.capacity import CAPACITY_OWNER_ID_PATTERN, CapacityOwnerKind
from shared.compute_enrollment import AgentCapacityState
from shared.compute_policy import (
    ComputeUnitRecord,
    MachinePool,
)
from shared.container_requests import OciRuntimeName, schedulable_capacity
from shared.contracts import ContractModel
from shared.scheduling import (
    SchedulerWorkerRecord,
    SchedulerWorkerStatus,
    WorkerUnavailableReason,
)
from shared.timestamps import utc_now

DEFAULT_AGENT_WORKER_BUILD_VERSION = "local"


class AgentPoolWorkerAction(StrEnum):
    Existing = "existing"
    Ensured = "ensured"
    Disabled = "disabled"
    Skipped = "skipped"


class AgentPoolConfig(ContractModel):
    workspace_id: str
    pool: MachinePool
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
    pool: MachinePool
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
        capacity_owner_id: str,
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

    def update_worker_tenancy(
        self,
        worker_id: str,
        *,
        workspace_id: str,
        owner_user_id: str,
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
            pool=self.config.pool,
        )
        for machine in self.machines.list_agent_token_states(
            self.config.workspace_id,
            self.config.capacity_owner_id,
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
            cordoned = not machine_accepts_work(machine)
            disabled = self.workers.disable_worker(
                worker.worker_id,
                reason=(
                    WorkerUnavailableReason.MachineCordoned
                    if cordoned
                    else WorkerUnavailableReason.AgentDisconnected
                ),
                now=current_time,
            )
            return AgentPoolWorkerResult(
                action=AgentPoolWorkerAction.Disabled,
                machine_id=machine.machine_id,
                worker_id=disabled.worker_id,
                reason=(
                    # A cordoned machine is reachable and healthy, so reporting it
                    # as disconnected sends the reader after a network fault that
                    # is not there.
                    f"agent machine is cordoned: {machine.capacity_reason}"
                    if cordoned
                    else "agent machine is not schedulable"
                ),
            )
        if worker is not None and worker.status is not SchedulerWorkerStatus.Unavailable:
            return self.reconcile_worker_tenancy(machine, worker, now=current_time)
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

    def reconcile_worker_tenancy(
        self,
        machine: ComputeAgentTokenState,
        worker: SchedulerWorkerRecord,
        *,
        now: datetime | None = None,
    ) -> AgentPoolWorkerResult:
        """Bring a live worker's tenancy back in step with its machine's enrollment.

        A worker registered before its machine had an account carries an empty owner,
        and an empty owner serves nobody, so nothing would place on it again without
        this. A narrow field update rather than a rewrite: the record also carries
        capacity and status a running worker is still changing.
        """
        if (
            worker.owner_user_id == machine.owner_user_id
            and worker.workspace_id == machine.workspace_id
        ):
            return AgentPoolWorkerResult(
                action=AgentPoolWorkerAction.Existing,
                machine_id=machine.machine_id,
                worker_id=worker.worker_id,
                reason="agent machine worker already exists",
            )
        updated = self.workers.update_worker_tenancy(
            worker.worker_id,
            workspace_id=machine.workspace_id,
            owner_user_id=machine.owner_user_id,
            now=now,
        )
        return AgentPoolWorkerResult(
            action=AgentPoolWorkerAction.Ensured,
            machine_id=machine.machine_id,
            worker_id=updated.worker_id,
            reason="agent machine worker tenancy reconciled",
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


def agent_pool_config_from_pool(pool: ComputeUnitRecord) -> AgentPoolConfig | None:
    """Config for a unit whose machines join, or None for one that provisions.

    The unit's capacity owner kind decides. A `WorkspaceAgent` unit owns no
    provider resources — its machines arrive by joining — so the agent pool
    controller drives it; a unit backed by an auto-scaling group is driven by
    the capacity controller instead.
    """
    if pool.capacity_owner_kind is not CapacityOwnerKind.WorkspaceAgent:
        return None
    return AgentPoolConfig(
        workspace_id=pool.workspace_id,
        pool=pool.pool,
        capacity_owner_id=pool.capacity_owner_id,
        gpu_type=pool.worker_gpu_type,
        worker_build_version=DEFAULT_AGENT_WORKER_BUILD_VERSION,
    )


def agent_pool_config_from_compute_state(state: ComputeUnitState) -> AgentPoolConfig:
    config = _pool_config_from_metadata(state)
    normalized = normalize_unit_config(config)
    return AgentPoolConfig(
        workspace_id=state.workspace_id,
        pool=state.pool,
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
    # What the scheduler may place onto, not what the machine physically holds:
    # the agent and the container runtime are already running on it.
    cpu_millicores = schedulable_capacity(machine.cpu_millicores or machine.cpu_count * 1000)
    memory_mib = schedulable_capacity(machine.memory_mb)
    gpu_types = _machine_gpu_types(machine, config)
    return SchedulerWorkerRecord(
        worker_id=agent_machine_worker_id(machine.machine_id),
        pool=config.pool,
        # The machine's own owner, not the controller's: a joined machine in a
        # group an auto-scaling unit also feeds carries the unit that issued its
        # credential, which is what keeps that unit's drain from terminating it.
        capacity_owner_id=machine.capacity_owner_id or config.capacity_owner_id,
        workspace_id=machine.workspace_id,
        owner_user_id=machine.owner_user_id,
        machine_id=machine.machine_id,
        status=SchedulerWorkerStatus.Pending,
        gpu_type=gpu_types[0] if gpu_types else "",
        runtime_class=OciRuntimeName.Runsc.value,
        runtime_classes=[OciRuntimeName.Runsc.value],
        private_worker=True,
        requires_pool_selector=True,
        free_cpu_millicores=cpu_millicores,
        free_memory_mib=memory_mib,
        free_gpu_count=machine.gpu_count,
        total_cpu_millicores=cpu_millicores,
        total_memory_mib=memory_mib,
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
        and _machine_owned_by(machine, config)
        and machine.executor == expected_executor
        and machine_accepts_work(machine)
        and agent_machine_connected(agent_telemetry_state(machine), now=now)
    )


def machine_accepts_work(machine: ComputeAgentTokenState) -> bool:
    """Whether a machine may still be given work it has not already been given.

    A cordoned machine is connected and healthy and must not be scheduled onto,
    which is the whole distinction between `Cordoned` and `Available`. Reading it
    here rather than only where the cordon is applied is what makes the cordon
    hold: `ensure_machine_worker` re-adds the worker of any schedulable machine
    whose worker is unavailable, so a cordon this function cannot see is undone by
    the next reconcile pass. Preemption will not reapply it either — it records
    its operation id and returns early on the second sight of the same one.

    The same argument the compute package makes about tenancy: a stamp survives
    because something reconciles it, not because it was written once.
    """
    return machine.capacity_state is AgentCapacityState.Available


def _machine_owned_by(machine: ComputeAgentTokenState, config: AgentPoolConfig) -> bool:
    """Whether this unit's controller owns the machine.

    Several units may feed one pool, so matching on the pool label alone would
    have every one of their controllers claim every machine in it. The worker id
    is derived from the machine, so they would each write the same worker with a
    different owner and alternate it on every reconcile.
    """
    if machine.capacity_owner_id:
        return machine.capacity_owner_id == config.capacity_owner_id
    return machine.pool == config.pool


def _machine_gpu_types(machine: ComputeAgentTokenState, config: AgentPoolConfig) -> list[str]:
    values: list[str] = []
    if config.gpu_type:
        values.append(config.gpu_type)
    values.extend(gpu for gpu in machine.gpus if gpu)
    return list(dict.fromkeys(values))


def _pool_config_from_metadata(state: ComputeUnitState) -> PoolConfig:
    raw_config = state.metadata.get("config")
    if isinstance(raw_config, dict):
        return PoolConfig.model_validate(raw_config)
    return PoolConfig(name=state.name)
