from __future__ import annotations

from datetime import UTC, datetime

from compute.state import (
    ComputeAgentTokenState,
    ComputeAgentWorkerSlotState,
    ComputeJoinTokenState,
    ComputeUnitState,
    RedisComputeStateRepository,
)
from coordination.redis_client import RedisClient
from shared.compute_policy import MachinePool, UnitName
from shared.routing import AgentBackendRoute
from shared.usage import UsageBillingOwner
from tests.redis_fakes import FakeRedis

OWNER_ID = "11111111-1111-4111-8111-111111111111"
ORPHAN_OWNER_ID = "33333333-3333-4333-8333-333333333333"


def test_compute_state_repository_tracks_pools_agents_slots_and_ttls() -> None:
    fake = FakeRedis()
    redis = RedisClient(fake, key_prefix="test")
    repo = RedisComputeStateRepository(redis)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    pool = ComputeUnitState(
        workspace_id="ws-1",
        name=UnitName("default"),
        capacity_owner_id=OWNER_ID,
        provider="agent",
        desired_machines=2,
        updated_at=now,
    )
    repo.save_unit_state(pool)
    assert repo.get_unit_state("ws-1", OWNER_ID) == pool
    assert repo.list_all_pool_states() == [pool]
    # No name appears in a hot-state key: the owner id is the only identity a
    # writer and a reader can agree on.
    assert repo.pool_lock_plan("ws-1", OWNER_ID).key == (
        f"test:compute:workspaces:ws-1:units:{OWNER_ID}:lock"
    )

    join = ComputeJoinTokenState(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        token_hash="join-hash",
        workspace_id="ws-1",
        pool=MachinePool("default"),
        created_at=now,
    )
    repo.save_join_token_state(join, ttl_seconds=0)
    assert repo.get_join_token_state("join-hash") == join
    assert fake.expirations["test:compute:join-tokens:join-hash"] == 1

    agent = ComputeAgentTokenState(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        token_hash="agent-hash",
        workspace_id="ws-1",
        pool=MachinePool("default"),
        machine_id="machine-1",
        created_at=now,
    )
    repo.save_agent_token_state(agent, ttl_seconds=60)
    assert repo.get_agent_token_state("agent-hash") == agent
    assert repo.get_agent_machine_state_for_workspace("ws-1", "machine-1") == agent

    fake.values.pop("test:compute:workspaces:ws-1:machines:machine-1:unit")
    assert repo.get_agent_machine_state_for_workspace("ws-1", "machine-1") == agent

    slot = ComputeAgentWorkerSlotState(
        billing_owner=UsageBillingOwner.SelfHosted,
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        workspace_id="ws-1",
        pool=MachinePool("default"),
        machine_id="machine-1",
        worker_id="worker-1",
        created_at=now,
        updated_at=now,
    )
    saved_slot = repo.save_agent_worker_slot_state(slot, now=now)
    assert repo.list_agent_worker_slot_states("ws-1", OWNER_ID, "machine-1") == [saved_slot]
    assert repo.delete_agent_worker_slot_state("ws-1", OWNER_ID, "machine-1", "worker-1")

    route = AgentBackendRoute(
        route_id="route-1",
        workspace_id="ws-1",
        pool=MachinePool("default"),
        capacity_owner_id=OWNER_ID,
        machine_id="machine-1",
        worker_id="worker-1",
        container_id="container-1",
        port=8080,
    )
    repo.save_agent_route_state(route)
    assert repo.list_agent_route_states("ws-1", OWNER_ID, "machine-1") == [route]
    assert fake.values[repo.keys.agent_route_revision("ws-1", OWNER_ID, "machine-1")] == "1"
    assert repo.delete_agent_route_state(
        workspace_id="ws-1",
        capacity_owner_id=OWNER_ID,
        machine_id="machine-1",
        route_id="route-1",
    )
    assert repo.list_agent_route_states("ws-1", OWNER_ID, "machine-1") == []

    fake.sets[repo.keys.agent_machine_index("ws-1", OWNER_ID)].add("machine-stale")
    assert repo.prune_agent_machine_index("ws-1", OWNER_ID) == 1

    repo.save_agent_worker_slot_state(slot, now=now)
    assert repo.delete_agent_machine_state("ws-1", OWNER_ID, "machine-1")
    assert repo.get_agent_token_state("agent-hash") is None
    assert repo.list_agent_worker_slot_states("ws-1", OWNER_ID, "machine-1") == []
    assert repo.delete_unit_state("ws-1", OWNER_ID)

    orphan_revision = repo.keys.agent_route_revision("ws-1", ORPHAN_OWNER_ID, "orphaned-machine")
    peer_revision = repo.keys.agent_route_revision("ws-peer", ORPHAN_OWNER_ID, "orphaned-machine")
    fake.set(orphan_revision, "1")
    fake.set(peer_revision, "1")

    assert repo.delete_unit_state("ws-1", ORPHAN_OWNER_ID)
    assert fake.get(orphan_revision) is None
    assert fake.get(peer_revision) == "1"

    cleanup_pool = ComputeUnitState(
        workspace_id="ws-1",
        name=UnitName("default"),
        capacity_owner_id=OWNER_ID,
        provider="agent",
    )
    cleanup_agent = ComputeAgentTokenState(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        token_hash="cleanup-agent-hash",
        workspace_id="ws-1",
        pool=MachinePool("cleanup"),
        machine_id="cleanup-machine",
        created_at=now,
    )
    cleanup_slot = ComputeAgentWorkerSlotState(
        billing_owner=UsageBillingOwner.SelfHosted,
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        workspace_id="ws-1",
        pool=MachinePool("cleanup"),
        machine_id="cleanup-machine",
        worker_id="cleanup-worker",
        created_at=now,
        updated_at=now,
    )
    cleanup_route = AgentBackendRoute(
        route_id="cleanup-route",
        workspace_id="ws-1",
        pool=MachinePool("cleanup"),
        machine_id="cleanup-machine",
        worker_id="cleanup-worker",
        container_id="cleanup-container",
        port=8080,
    )
    repo.save_unit_state(cleanup_pool)
    repo.save_agent_token_state(cleanup_agent)
    repo.save_agent_worker_slot_state(cleanup_slot, now=now)
    repo.save_agent_route_state(cleanup_route)

    assert repo.delete_unit_state("ws-1", OWNER_ID)
    assert repo.get_unit_state("ws-1", OWNER_ID) is None
    assert repo.get_agent_token_state("cleanup-agent-hash") is None
    assert repo.list_agent_token_states("ws-1", OWNER_ID) == []
    assert repo.list_agent_worker_slot_states("ws-1", OWNER_ID, "cleanup-machine") == []
    assert repo.list_agent_route_states("ws-1", OWNER_ID, "cleanup-machine") == []


def test_compute_state_repository_deletes_exact_workspace_residue() -> None:
    fake = FakeRedis()
    repo = RedisComputeStateRepository(RedisClient(fake, key_prefix="test"))
    now = datetime(2026, 1, 1, tzinfo=UTC)

    for workspace_id, suffix in (("ws-delete", "owned"), ("ws-peer", "peer")):
        pool = f"pool-{suffix}"
        machine_id = f"machine-{suffix}"
        repo.save_join_token_state(
            ComputeJoinTokenState(
                capacity_owner_id="11111111-1111-4111-8111-111111111111",
                token_hash=f"join-{suffix}",
                workspace_id=workspace_id,
                pool=MachinePool(pool),
                created_at=now,
            )
        )
        repo.save_agent_token_state(
            ComputeAgentTokenState(
                capacity_owner_id="11111111-1111-4111-8111-111111111111",
                token_hash=f"agent-{suffix}",
                workspace_id=workspace_id,
                pool=MachinePool(pool),
                machine_id=machine_id,
                created_at=now,
            )
        )
        fake.set(repo.keys.agent_route_revision(workspace_id, pool, machine_id), "1")

    assert repo.delete_workspace_state("ws-delete") > 0
    assert repo.get_join_token_state("join-owned") is None
    assert repo.get_agent_token_state("agent-owned") is None
    assert repo.redis.scan("test:compute:workspaces:ws-delete:*") == []

    assert repo.get_join_token_state("join-peer") is not None
    assert repo.get_agent_token_state("agent-peer") is not None
    assert repo.redis.scan("test:compute:workspaces:ws-peer:*") != []
