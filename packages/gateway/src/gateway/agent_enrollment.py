from __future__ import annotations

from dataclasses import dataclass

from compute.projection import PoolConfig, PrivateUnitState
from compute.state import ComputeAgentTokenState, ComputeJoinTokenState
from shared.compute_policy import ComputeUnitRecord

from gateway.http import JoinAgentResponse
from gateway.views import pool_config_from_unit


@dataclass(frozen=True, slots=True, repr=False)
class AgentJoinResult:
    response: JoinAgentResponse
    agent_state: ComputeAgentTokenState
    unit: ComputeUnitRecord
    pool_state: PrivateUnitState
    pool_config_update: PoolConfig | None = None
    join_token_updates: tuple[tuple[ComputeJoinTokenState, int], ...] = ()
    previous_token_hash: str = ""
    emit_join_event: bool = False


def private_unit_for_enrollment(
    unit: ComputeUnitRecord, *, created_by_token_id: str = "gateway"
) -> PrivateUnitState:
    config = pool_config_from_unit(unit)
    return PrivateUnitState(
        workspace_id=unit.workspace_id,
        name=unit.name,
        pool=unit.pool,
        capacity_owner_id=unit.capacity_owner_id,
        selector=config.selector,
        config=config,
        status=unit.status,
        created_by_token_id=created_by_token_id,
        reserved_nodes=unit.max_machines,
    )
