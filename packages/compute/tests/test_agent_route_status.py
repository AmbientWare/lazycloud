from __future__ import annotations

from compute.agent_control import AgentRouteStatusRequest, plan_route_status_update
from compute.state import ComputeAgentTokenState
from shared.compute_policy import MachinePool
from shared.routing import AgentBackendRoute, BackendRouteState


def _agent() -> ComputeAgentTokenState:
    return ComputeAgentTokenState(
        token_hash="hash",
        workspace_id="ws-1",
        capacity_owner_id="unit-1",
        pool=MachinePool("default"),
        machine_id="machine-1",
    )


def test_reporting_a_route_that_is_already_gone_is_not_an_error() -> None:
    """A container outlives its route by a moment, and the agent says so.

    The agent reports on routes it was streamed, so a container exiting between
    the stream and the report is ordinary. Refusing it raised through the whole
    daemon and exited the process, and every restart replayed the same report
    against the same missing route — one route the control plane would not
    accept stopped the agent from serving any of them.
    """
    plan = plan_route_status_update(
        _agent(),
        None,
        AgentRouteStatusRequest(route_id="route-1", state=BackendRouteState.Ready),
    )

    assert plan.accepted
    assert plan.already_gone
    # Nothing to write: the record and the report already agree.
    assert plan.updated is None
    assert not plan.should_save


def test_a_route_owned_by_another_agent_is_still_refused() -> None:
    """Tolerating an absent route must not tolerate reaching across a tenant."""
    plan = plan_route_status_update(
        _agent(),
        AgentBackendRoute(
            route_id="route-1",
            workspace_id="ws-2",
            pool=MachinePool("default"),
            machine_id="machine-1",
        ),
        AgentRouteStatusRequest(route_id="route-1", state=BackendRouteState.Ready),
    )

    assert not plan.accepted
    assert not plan.already_gone
