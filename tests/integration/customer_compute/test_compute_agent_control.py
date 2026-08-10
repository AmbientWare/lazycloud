from __future__ import annotations

from datetime import UTC, datetime, timedelta

from compute.agent_control import (
    AgentJoinRequest,
    JoinTokenDecision,
    hash_compute_token,
    plan_agent_join,
    plan_join_token_binding,
)
from compute.projection import PoolConfig, PrivateUnitState
from compute.state import (
    ComputeJoinTokenState,
)
from shared.compute_enrollment import (
    ComputePreflightCheck,
    PreflightSeverity,
)
from shared.compute_policy import MachinePool, UnitName


def test_join_token_binding_and_agent_join_gpu_locking() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    token = ComputeJoinTokenState(
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        token_hash=hash_compute_token("join-token"),
        owner_user_id="22222222-2222-4222-8222-222222222222",
        workspace_id="workspace-one",
        pool=MachinePool("gpu-pool"),
        machine_id="machine-fixed",
        created_by_token_id="token-owner",
        expires_at=now + timedelta(hours=1),
    )

    binding = plan_join_token_binding(token, "fingerprint-one", now=now)
    assert binding.accepted
    assert binding.should_save
    assert binding.ttl_seconds == 3600
    assert binding.state is not None
    assert binding.state.bound_fingerprint == "fingerprint-one"

    conflict = plan_join_token_binding(binding.state, "fingerprint-two", now=now)
    assert not conflict.accepted
    assert conflict.decision is JoinTokenDecision.FingerprintConflict

    pool = PrivateUnitState(
        workspace_id="workspace-one",
        name=UnitName("gpu-unit"),
        pool=MachinePool("gpu-pool"),
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        config=PoolConfig(name="gpu-unit"),
        created_by_token_id="token-owner",
    )
    request = AgentJoinRequest(
        machine_fingerprint="fingerprint-one",
        hostname="agent-host",
        cpu_count=4,
        memory_mb=8192,
        gpu=["NVIDIA RTX A4000"],
        gpu_ids=["GPU-0"],
        gpu_count=1,
        preflight=[
            ComputePreflightCheck(
                name="container-runtime",
                ok=True,
                severity=PreflightSeverity.Error,
            )
        ],
    )
    join = plan_agent_join(token, pool, request, agent_token="agent-token", now=now)

    assert join.accepted
    assert join.machine_id == "machine-fixed"
    assert join.agent_state is not None
    assert join.agent_state.token_hash == hash_compute_token("agent-token")
    assert join.agent_state.cpu_millicores == 4000
    assert join.agent_state.preflight_passed
    assert not join.agent_state.heartbeat_confirmed
    assert not join.agent_state.schedulable
    assert join.agent_state.metadata["pool_transport"] == "tsnet_restricted"
    assert join.agent_state.metadata["pool_mode"] == "private"
    assert join.pool_config_update is not None
    assert join.pool_config_update.gpu == ["A4000"]
    assert join.should_save_agent
    assert join.should_save_pool
    assert join.should_register_pool

    rtx_token = token.model_copy(update={"pool": "rtx-pool", "machine_id": "rtx-machine"})
    rtx_pool = pool.model_copy(update={"name": "rtx-pool", "config": PoolConfig(name="rtx-pool")})
    rtx_request = request.model_copy(
        update={
            "machine_fingerprint": "fingerprint-rtx",
            "gpu": ["NVIDIA GeForce RTX 4090"],
            "gpu_ids": ["GPU-1"],
        }
    )
    rtx_join = plan_agent_join(rtx_token, rtx_pool, rtx_request, now=now)
    assert rtx_join.accepted
    assert rtx_join.pool_config_update is not None
    assert rtx_join.pool_config_update.gpu == ["RTX4090"]

    mismatch_pool = pool.model_copy(update={"config": PoolConfig(name="gpu-pool", gpu=["H100"])})
    rejected = plan_agent_join(token, mismatch_pool, request, agent_token="agent-token", now=now)
    assert not rejected.accepted
    assert "requires GPU type" in rejected.err_msg
