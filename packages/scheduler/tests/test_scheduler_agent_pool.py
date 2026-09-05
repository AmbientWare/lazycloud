from __future__ import annotations

from datetime import UTC, datetime, timedelta

from compute.agent_control import DEFAULT_PRIVATE_EXECUTOR, agent_machine_worker_id
from compute.state import ComputeAgentTokenState
from scheduler.agent_pool import (
    AgentPoolConfig,
    AgentPoolWorkerAction,
    AgentWorkerPoolController,
)
from scheduler.fleet import SchedulerWorkerStatus
from scheduler.state import RedisSchedulerWorkerRepository, SchedulerWorkerRecord
from shared.compute_enrollment import AgentCapacityState, ComputePreflightCheck
from shared.compute_policy import MachinePool
from shared.container_requests import schedulable_capacity
from shared.scheduling import WorkerUnavailableReason
from tests.real_redis import RealRedisActors


def test_agent_worker_pool_reconciles_connected_machine_and_capacity(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    machine = _agent_machine(
        machine_id="machine-one",
        cpu_millicores=4000,
        memory_mb=8192,
        gpus=["A4000"],
        gpu_count=1,
        last_heartbeat_at=now,
    )
    workers = RedisSchedulerWorkerRepository(real_redis_actors.client())
    controller = AgentWorkerPoolController(
        AgentPoolConfig(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            workspace_id="ws-1",
            pool=MachinePool("gpu"),
            gpu_type="A4000",
        ),
        _MachineRepo([machine]),
        workers,
    )

    reconciled = controller.reconcile(now=now)

    worker_id = agent_machine_worker_id("machine-one")
    assert reconciled.ensured_worker_ids == [worker_id]
    worker = workers.get_worker(worker_id)
    assert worker is not None
    assert worker.status is SchedulerWorkerStatus.Pending
    assert worker.pool == "gpu"
    assert worker.machine_id == "machine-one"
    # Less than the machine physically holds. The agent and the container
    # runtime are already on it, and offer selection bought it on that basis.
    assert worker.total_cpu_millicores == schedulable_capacity(4000)
    assert worker.total_memory_mib == schedulable_capacity(8192)
    # Cards are discrete and are not shared with the platform, so they are not
    # reduced.
    assert worker.total_gpu_count == 1
    assert worker.gpu_type == "A4000"


def test_agent_worker_pool_excludes_machine_with_failed_typed_preflight(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    machine = _agent_machine(
        machine_id="machine-one",
        cpu_millicores=4000,
        memory_mb=8192,
        last_heartbeat_at=now,
    ).model_copy(
        update={
            "preflight_passed": False,
            "schedulable": False,
            "preflight": [
                ComputePreflightCheck(name="container_engine", ok=False, message="unavailable")
            ],
        }
    )
    workers = RedisSchedulerWorkerRepository(real_redis_actors.client())
    controller = AgentWorkerPoolController(
        AgentPoolConfig(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            workspace_id="ws-1",
            pool=MachinePool("gpu"),
        ),
        _MachineRepo([machine]),
        workers,
    )

    reconciled = controller.reconcile(now=now)

    assert reconciled.skipped_machine_ids == ["machine-one"]
    assert workers.get_worker(agent_machine_worker_id("machine-one")) is None


def test_agent_worker_pool_disables_stale_machine_worker(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    machine = _agent_machine(
        machine_id="machine-one",
        cpu_millicores=4000,
        memory_mb=8192,
        last_heartbeat_at=now - timedelta(minutes=2),
    )
    worker = SchedulerWorkerRecord(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        worker_id=agent_machine_worker_id("machine-one"),
        pool=MachinePool("gpu"),
        machine_id="machine-one",
        status=SchedulerWorkerStatus.Available,
        total_cpu_millicores=4000,
        total_memory_mib=8192,
        free_cpu_millicores=4000,
        free_memory_mib=8192,
        created_at=now,
        updated_at=now,
    )
    workers = RedisSchedulerWorkerRepository(real_redis_actors.client())
    workers.add_worker(worker, now=now)
    controller = AgentWorkerPoolController(
        AgentPoolConfig(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            workspace_id="ws-1",
            pool=MachinePool("gpu"),
        ),
        _MachineRepo([machine]),
        workers,
    )

    disabled = controller.ensure_machine_worker(machine, now=now)
    unavailable_worker = workers.get_worker(worker.worker_id)
    assert unavailable_worker is not None

    assert disabled.action is AgentPoolWorkerAction.Disabled
    assert unavailable_worker.status is SchedulerWorkerStatus.Unavailable


def test_agent_worker_pool_does_not_readd_a_cordoned_machine_worker(
    real_redis_actors: RealRedisActors,
) -> None:
    """A cordon has to survive the pass that would otherwise undo it.

    `ensure_machine_worker` re-adds the worker of any schedulable machine whose
    worker is unavailable, and preemption records its operation id and returns
    early the second time it sees it. So a cordon the schedulability rule cannot
    see is applied once by preemption and removed by the very next reconcile,
    which puts work back on a machine that is being taken away.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    machine = _agent_machine(
        machine_id="machine-one",
        cpu_millicores=4000,
        memory_mb=8192,
        last_heartbeat_at=now,
        capacity_state=AgentCapacityState.Cordoned,
    )
    worker = SchedulerWorkerRecord(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        worker_id=agent_machine_worker_id("machine-one"),
        pool=MachinePool("gpu"),
        machine_id="machine-one",
        status=SchedulerWorkerStatus.Unavailable,
        total_cpu_millicores=4000,
        total_memory_mib=8192,
        free_cpu_millicores=4000,
        free_memory_mib=8192,
        created_at=now,
        updated_at=now,
    )
    workers = RedisSchedulerWorkerRepository(real_redis_actors.client())
    workers.add_worker(worker, now=now)
    controller = AgentWorkerPoolController(
        AgentPoolConfig(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            workspace_id="ws-1",
            pool=MachinePool("gpu"),
        ),
        _MachineRepo([machine]),
        workers,
    )

    outcome = controller.ensure_machine_worker(machine, now=now)

    settled = workers.get_worker(worker.worker_id)
    assert settled is not None
    assert settled.status is SchedulerWorkerStatus.Unavailable
    assert outcome.action is not AgentPoolWorkerAction.Ensured


def test_agent_worker_pool_reports_a_cordon_as_a_cordon(
    real_redis_actors: RealRedisActors,
) -> None:
    """A cordoned machine is connected, so the disconnected diagnosis misleads."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    machine = _agent_machine(
        machine_id="machine-one",
        cpu_millicores=4000,
        memory_mb=8192,
        last_heartbeat_at=now,
        capacity_state=AgentCapacityState.Cordoned,
    )
    worker = SchedulerWorkerRecord(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        worker_id=agent_machine_worker_id("machine-one"),
        pool=MachinePool("gpu"),
        machine_id="machine-one",
        status=SchedulerWorkerStatus.Available,
        total_cpu_millicores=4000,
        total_memory_mib=8192,
        free_cpu_millicores=4000,
        free_memory_mib=8192,
        created_at=now,
        updated_at=now,
    )
    workers = RedisSchedulerWorkerRepository(real_redis_actors.client())
    workers.add_worker(worker, now=now)
    controller = AgentWorkerPoolController(
        AgentPoolConfig(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            workspace_id="ws-1",
            pool=MachinePool("gpu"),
        ),
        _MachineRepo([machine]),
        workers,
    )

    outcome = controller.ensure_machine_worker(machine, now=now)

    settled = workers.get_worker(worker.worker_id)
    assert settled is not None
    assert outcome.action is AgentPoolWorkerAction.Disabled
    assert settled.unavailable_reason is WorkerUnavailableReason.MachineCordoned


def test_machine_drain_survives_agent_pool_reconciliation(
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    machine = _agent_machine(
        machine_id="machine-one",
        cpu_millicores=4000,
        memory_mb=8192,
        last_heartbeat_at=now,
        capacity_state=AgentCapacityState.Draining,
    )
    workers = RedisSchedulerWorkerRepository(real_redis_actors.client())
    worker_id = agent_machine_worker_id(machine.machine_id)
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=worker_id,
            capacity_owner_id=machine.capacity_owner_id,
            machine_id=machine.machine_id,
            pool=machine.pool,
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=4000,
            total_memory_mib=8192,
            free_cpu_millicores=3000,
            free_memory_mib=4096,
        ),
        now=now,
    )
    controller = AgentWorkerPoolController(
        AgentPoolConfig(
            capacity_owner_id=machine.capacity_owner_id,
            workspace_id=machine.workspace_id,
            pool=machine.pool,
        ),
        _MachineRepo([machine]),
        workers,
    )

    controller.reconcile(now=now)
    draining = workers.get_worker(worker_id)
    assert draining is not None
    assert draining.status is SchedulerWorkerStatus.Draining
    controller.reconcile(now=now + timedelta(seconds=1))
    assert workers.get_worker(worker_id) == draining


class _MachineRepo:
    def __init__(self, machines: list[ComputeAgentTokenState]) -> None:
        self.machines = machines

    def list_agent_token_states(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> list[ComputeAgentTokenState]:
        return [
            machine
            for machine in self.machines
            if machine.workspace_id == workspace_id
            and machine.capacity_owner_id == capacity_owner_id
        ]


def _agent_machine(
    *,
    machine_id: str,
    cpu_millicores: int,
    memory_mb: int,
    gpus: list[str] | None = None,
    gpu_count: int = 0,
    last_heartbeat_at: datetime,
    capacity_state: AgentCapacityState = AgentCapacityState.Available,
) -> ComputeAgentTokenState:
    return ComputeAgentTokenState(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        token_hash=f"token-{machine_id}",
        workspace_id="ws-1",
        pool=MachinePool("gpu"),
        machine_id=machine_id,
        executor=DEFAULT_PRIVATE_EXECUTOR,
        cpu_millicores=cpu_millicores,
        memory_mb=memory_mb,
        gpus=gpus or [],
        gpu_count=gpu_count,
        preflight_passed=True,
        heartbeat_confirmed=True,
        schedulable=True,
        capacity_state=capacity_state,
        last_join_at=last_heartbeat_at,
        last_heartbeat_at=last_heartbeat_at,
    )
