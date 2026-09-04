from __future__ import annotations

from compute.agent_control import (
    AgentWorkerTokenPlan,
    agent_machine_worker_id,
    plan_agent_worker_slot,
)
from compute.state import ComputeAgentTokenState
from shared.compute_enrollment import AgentCapacityState
from shared.compute_policy import MachinePool
from shared.usage import UsageBillingOwner


def test_enrolled_machine_owns_runtime_independently_of_placement_availability() -> None:
    machine = ComputeAgentTokenState(
        token_hash="test-hash",
        workspace_id="workspace-one",
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        machine_id="machine-one",
        pool=MachinePool("cpu"),
        cpu_millicores=2000,
        memory_mb=4096,
        capacity_state=AgentCapacityState.Draining,
        schedulable=False,
    )
    token = AgentWorkerTokenPlan(accepted=True, worker_token_id="token-one")
    plan = plan_agent_worker_slot(
        machine,
        [],
        token,
        billing_owner=UsageBillingOwner.PlatformFleet,
        cluster_name="test",
        worker_image="worker:one",
    )

    assert plan.accepted
    assert plan.slot is not None
    assert plan.slot.worker_id == agent_machine_worker_id(machine.machine_id)
    assert plan.slot.worker_token_id == token.worker_token_id
    assert plan.pruned_worker_ids == []

    regenerated = plan_agent_worker_slot(
        machine,
        [plan.slot],
        token,
        billing_owner=UsageBillingOwner.PlatformFleet,
        cluster_name="test",
        worker_image="worker:one",
    )
    assert regenerated.slot is not None
    assert regenerated.slot.worker_id == plan.slot.worker_id
    assert regenerated.slot.worker_token_id == plan.slot.worker_token_id
    assert regenerated.slot.created_at == plan.slot.created_at
    assert regenerated.pruned_worker_ids == []
