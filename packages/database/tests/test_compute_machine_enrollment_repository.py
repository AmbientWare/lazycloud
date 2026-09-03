from __future__ import annotations

from uuid import uuid4

from api.server.services import ApiServices
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
)
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import MachineRepository
from shared.compute_enrollment import AgentCapacityState
from shared.compute_fleet import Machine
from shared.compute_policy import MachinePool
from shared.timestamps import utc_now


def test_active_capacity_interruptions_return_only_preempting_machines(
    isolated_services: ApiServices,
) -> None:
    pool = MachinePool("interruption-test")
    now = utc_now()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        owner = WorkspaceMemberRepository(session).owner(workspace_id)
        assert owner is not None
        machines = MachineRepository(session)
        enrollments = ComputeMachineEnrollmentRepository(session)
        for state in (AgentCapacityState.Preempting, AgentCapacityState.Available):
            machine_id = str(uuid4())
            machines.upsert(
                Machine(id=machine_id, pool=pool, provider="aws"),
                workspace_id=workspace_id,
            )
            enrollments.create(
                ComputeMachineEnrollmentCreate(
                    user_id=owner.user_id,
                    workspace_id=workspace_id,
                    capacity_owner_id=str(uuid4()),
                    pool=pool,
                    machine_id=machine_id,
                    machine_fingerprint_hash=uuid4().hex,
                    credential_hash=uuid4().hex,
                    capacity_state=state,
                    capacity_reason="provider interruption",
                    capacity_observed_at=now,
                    last_join_at=now,
                )
            )

        [interruption] = enrollments.list_active_capacity_interruptions()

    assert interruption.state is AgentCapacityState.Preempting
    assert interruption.reason == "provider interruption"
    assert interruption.observed_at == now
