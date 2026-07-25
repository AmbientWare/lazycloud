from __future__ import annotations

from agent.service_manager import AgentServiceSpec, render_systemd_unit


def test_agent_service_retries_for_as_long_as_the_machine_exists() -> None:
    """A machine that stops retrying is billed capacity that does nothing.

    The unit previously carried a start rate limit, so five failures inside five
    minutes stopped the agent permanently. A public gateway can be unreachable
    for far longer than that, and the machine keeps costing money the whole time
    with no agent, no worker, and no capacity the scheduler can use.
    """
    unit = render_systemd_unit(
        AgentServiceSpec(binary_path="/usr/local/bin/lazycloud-agent", args=["join"])
    )

    assert "Restart=always" in unit
    assert "StartLimitIntervalSec=0" in unit
    assert "StartLimitBurst" not in unit
