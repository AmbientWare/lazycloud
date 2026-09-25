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


def test_a_route_change_replaces_the_rules_that_named_the_old_interface() -> None:
    """A host that wakes on a new interface, and without IPv6, keeps no rule for the old ones."""
    routes = {"-4": "default via 10.0.0.1 dev eth0", "-6": "default via fe80::1 dev eth0"}
    ran: list[list[str]] = []

    def run(args: list[str]) -> ProcessResult:
        ran.append(args)
        stdout = routes[args[1]] if args[2:4] == ["route", "show"] else ""
        # A check (-C) finds no rule, so each ensure adds one.
        return ProcessResult(
            args=args, exit_code=1 if "-C" in args else 0, stdout=stdout, stderr=""
        )

    backend = AgentBridgeNetworkBackend(
        ip_allocator=_Allocator(),
        # The prepared network pool runs `ip` itself; `true` stands in for it.
        config=AgentBridgeNetworkConfig(ip_binary="true"),
        system=CommandNetworkSystem(run_command=run),
    )
    backend.prepare()
    try:
        routes.update({"-4": "default via 10.0.1.1 dev ens5", "-6": ""})
        ran.clear()

        assert backend.refresh_capabilities()
        assert not backend.refresh_capabilities()
    finally:
        backend.close()

    deleted = [args for args in ran if "-D" in args]
    added = [args for args in ran if "-A" in args or "-I" in args]
    assert deleted
    assert all(args[0] == "ip6tables" or "eth0" in args for args in deleted)
    assert any(args[0] == "ip6tables" and "DROP" in args for args in deleted)
    assert all(args[0] == "iptables" for args in added)
    assert any("ens5" in args and "MASQUERADE" in args for args in added)
