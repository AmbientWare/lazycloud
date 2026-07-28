from __future__ import annotations

from agent_app.metrics import (
    CGROUP_V2_MEMORY_CURRENT,
    CGROUP_V2_MEMORY_MAX,
    agent_cpu_utilization_pct,
    agent_memory_sample,
    agent_network_sample,
)


def test_agent_memory_sample_uses_cgroup_limit_when_lower_than_host() -> None:
    meminfo_text = "\n".join(
        [
            "MemTotal:       1048576 kB",
            "MemAvailable:    524288 kB",
        ]
    )
    sample = agent_memory_sample(
        meminfo_text=meminfo_text,
        cgroup_files={
            CGROUP_V2_MEMORY_MAX: str(256 * 1024 * 1024),
            CGROUP_V2_MEMORY_CURRENT: str(64 * 1024 * 1024),
        },
    )

    assert sample.total_mb == 256
    assert sample.used_mb == 64
    assert sample.utilization_pct == 25.0


def test_agent_cpu_utilization_normalizes_load_average() -> None:
    assert agent_cpu_utilization_pct(cpu_count=4, load_average=2.0) == 50.0
    assert agent_cpu_utilization_pct(cpu_count=2, load_average=5.0) == 100.0


def test_agent_network_sample_sums_non_loopback_interfaces() -> None:
    sample = agent_network_sample(
        netdev_text="""
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes packets
    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0
  eth0: 200 2 0 0 0 0 0 0 300 3 0 0 0 0 0 0
  eth1: 400 4 0 0 0 0 0 0 500 5 0 0 0 0 0 0
"""
    )

    assert sample.recv_bytes == 600
    assert sample.sent_bytes == 800
    assert sample.recv_packets == 6
    assert sample.sent_packets == 8
