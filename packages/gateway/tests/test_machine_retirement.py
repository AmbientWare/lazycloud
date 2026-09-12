from __future__ import annotations

from uuid import uuid4

from api.server.services import ApiServices
from compute.agent_control import agent_machine_worker_id
from control.service import ControlPlaneService
from database.repositories.compute import ComputeMachineEnrollmentRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from gateway.http import JoinAgentRequest, LeaveAgentRequest
from operations.container_shutdown import DatabaseDurableWorkerAbsence
from scheduler.state import RedisSchedulerWorkerRepository
from shared.compute_fleet import ResourceStatus
from shared.compute_policy import UnitName
from shared.container_requests import ContainerShutdownTarget
from shared.containers import ContainerRecord, ContainerStatus
from shared.scheduling import SchedulerWorkerRecord
from tests.real_redis import RealRedisActors
from tests.workspaces import owned_workspace


def test_machine_retirement_preserves_cleanup_evidence_after_repeated_deletion(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    services = isolated_services
    workspace = owned_workspace(ControlPlaneService(services.context), "machine-retirement")
    unit = services.compute.create_unit(
        UnitName("retirement"), provider="agent", workspace=workspace.id
    )
    gateway = services.gateway_service
    credential = gateway.unit_state_coordinator.create_unit_join_token(
        unit, workspace_id=workspace.id, owner_token_id="retirement-owner"
    )
    agent = gateway.join_agent(
        JoinAgentRequest(
            join_token=credential.token,
            machine_fingerprint="retirement-host",
            hostname="retirement-host",
            os="linux",
            arch="amd64",
            cpu_count=2,
            memory_mb=4096,
        )
    )
    worker_id = agent_machine_worker_id(agent.machine_id)
    container_id = str(uuid4())
    workers = RedisSchedulerWorkerRepository(real_redis_actors.client())
    absence = DatabaseDurableWorkerAbsence(services.context, workers)
    with services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="retired-host-container",
                image="",
                command=[],
                workspace_id=workspace.id,
                machine_id=agent.machine_id,
                worker_id=worker_id,
                runtime_machine_id=agent.machine_id,
                runtime_worker_id=worker_id,
                status=ContainerStatus.Stopped,
            )
        )
    assert workers.get_worker(worker_id) is None
    assert not absence.is_absent(worker_id)
    assert not absence.is_absent(str(uuid4()))

    gateway.leave_agent(LeaveAgentRequest(agent_token=agent.agent_token))
    gateway.delete_machine(agent.machine_id, workspace_id=workspace.id)

    with services.context.database.session() as session:
        worker = WorkerRepository(session).get(worker_id, workspace_id=workspace.id)
        machine = MachineRepository(session).get(agent.machine_id, workspace_id=workspace.id)
        assert worker is not None and worker.status is ResourceStatus.Deleted
        assert worker.machine_id == agent.machine_id
        assert machine is not None and machine.status is ResourceStatus.Deleted
        assert (
            ComputeMachineEnrollmentRepository(session).list_for_unit(
                workspace.id, unit.capacity_owner_id
            )
            == []
        )
        container = ContainerRepository(session).records.get(
            container_id, workspace_id=workspace.id
        )
        assert container is not None
        assert container.worker_id == worker_id and container.machine_id == agent.machine_id
        assert not ContainerRepository(session).storage_is_released(
            container_id, worker_id=worker_id
        )
    assert services.compute.list_machines(workspace=workspace.id) == []
    assert all(worker.id != worker_id for worker in services.compute.list_workers())
    assert absence.is_absent(worker_id)

    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=worker_id,
            machine_id=agent.machine_id,
            pool=agent.pool,
            capacity_owner_id=unit.capacity_owner_id,
        )
    )
    assert not absence.is_absent(worker_id)
    workers.remove_worker(worker_id)
    services.container_shutdowns.confirm(
        [ContainerShutdownTarget(container_id=container_id, worker_id=worker_id)],
        timeout_seconds=0.1,
    )
