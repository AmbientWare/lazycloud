from __future__ import annotations

from agent.operations import AgentRouteProxy, plan_agent_route_proxy


def test_agent_route_proxy_prefers_tailnet_hostname_when_present() -> None:
    proxy = AgentRouteProxy(
        agent_id="agent_1",
        target_host="127.0.0.1",
        target_port=9100,
        public_path="/agent/agent_1",
        tailnet_hostname="agent-1.tailnet",
    )
    plan = plan_agent_route_proxy(proxy)
    assert plan.tailnet_peer is not None
    assert plan.dial_plan.via_tailnet_peer == "agent_1"
    assert plan.dial_plan.target.url == "http://agent-1.tailnet:9100/agent/agent_1"
    assert plan.dial_plan.metadata["target_host"] == "127.0.0.1"
