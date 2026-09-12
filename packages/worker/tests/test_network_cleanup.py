from __future__ import annotations

import pytest
from foundation.process import ProcessResult
from worker.network_backend import (
    AgentBridgeNetworkBackend,
    AgentBridgeNetworkConfig,
    CommandNetworkSystem,
    ProbeNetworkReservation,
)


class _Allocator:
    worker_id = "worker-1"

    def acquire_network_lock(self) -> str:
        return "token"

    def release_network_lock(self, token: str) -> None:
        return None

    def reserve_container_ip(self, container_id: str) -> str:
        return "192.168.0.2"

    def release_container_ip(self, container_id: str) -> None:
        return None

    def reserve_probe_ip(self) -> ProbeNetworkReservation:
        return ProbeNetworkReservation(ip_address="192.168.0.2", reservation_id=self.worker_id)

    def release_probe_ip(self, reservation: ProbeNetworkReservation) -> None:
        return None


def test_network_cleanup_retains_ip_when_owned_interface_cannot_be_removed() -> None:
    released: list[str] = []

    class Allocator(_Allocator):
        def release_container_ip(self, container_id: str) -> None:
            released.append(container_id)

    def cleanup_commands(args: list[str]) -> ProcessResult:
        failed = args[:3] == ["ip", "link", "delete"]
        return ProcessResult(
            args=args,
            exit_code=1 if failed else 0,
            stdout="",
            stderr="RTNETLINK answers: Operation not permitted" if failed else "",
        )

    backend = AgentBridgeNetworkBackend(
        ip_allocator=Allocator(),
        config=AgentBridgeNetworkConfig(enable_ipv6=False),
        system=CommandNetworkSystem(run_command=cleanup_commands),
        assigned_ips={"container-1": "192.168.0.2"},
    )
    with pytest.raises(ExceptionGroup, match="network cleanup failed"):
        backend.teardown_network("container-1")

    assert backend.container_ip("container-1") == "192.168.0.2"
    assert not released
