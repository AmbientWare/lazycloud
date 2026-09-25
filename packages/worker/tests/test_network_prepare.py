from __future__ import annotations

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


def test_preparing_again_leaves_the_firewall_alone() -> None:
    """Readiness runs again on every re-registration, with containers alive.

    Inserting the bridge's ACCEPT rules again would put them above a network-
    blocked container's DROP rules, so only the first preparation touches them.
    """
    ran: list[list[str]] = []

    def run(args: list[str]) -> ProcessResult:
        ran.append(args)
        stdout = "default via 10.0.0.1 dev eth0" if args[2:4] == ["route", "show"] else ""
        return ProcessResult(
            args=args, exit_code=1 if "-C" in args else 0, stdout=stdout, stderr=""
        )

    backend = AgentBridgeNetworkBackend(
        ip_allocator=_Allocator(),
        # The prepared network pool runs `ip` itself; `true` stands in for it.
        config=AgentBridgeNetworkConfig(ip_binary="true", enable_ipv6=False),
        system=CommandNetworkSystem(run_command=run),
    )
    try:
        backend.prepare()
        assert any("-I" in args for args in ran)
        ran.clear()
        backend.prepare()
    finally:
        backend.close()

    assert not any(args[0] in {"iptables", "ip6tables"} for args in ran)
