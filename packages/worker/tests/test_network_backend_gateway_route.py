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


def _host_with_wireguard(args: list[str]) -> ProcessResult:
    stdout = ""
    if args[:4] == ["ip", "-4", "route", "show"]:
        stdout = "default via 10.84.1.1 dev enp39s0 proto dhcp src 10.84.1.103 metric 512\n"
    elif args[:4] == ["ip", "-4", "route", "get"]:
        stdout = "100.96.0.2 dev wg-lazycloud src 100.125.130.220 uid 0\n    cache\n"
    elif args[:3] == ["ip", "-6", "route"]:
        return ProcessResult(args=args, exit_code=2, stdout="", stderr="no ipv6")
    elif args[-2:] == ["rt_br0", "up"] or "-C" in args:
        # Firewall checks report the rule absent; everything else succeeds.
        return ProcessResult(args=args, exit_code=1 if "-C" in args else 0, stdout="", stderr="")
    return ProcessResult(args=args, exit_code=0, stdout=stdout, stderr="")


def test_bridge_forwards_and_masquerades_toward_the_control_plane_interface() -> None:
    """A managed node reaches the control plane over WireGuard, not its uplink.

    Container traffic to the control plane leaves through the tunnel interface,
    so forwarding and NAT installed only for the default-route interface leave
    the packet at the FORWARD policy, which drops it. Every container on the
    node then fails to reach the control plane, and so does the worker's own
    readiness probe, which is what kept every managed worker restarting.
    """
    system = CommandNetworkSystem(run_command=_host_with_wireguard)
    backend = AgentBridgeNetworkBackend(
        ip_allocator=_Allocator(),
        config=AgentBridgeNetworkConfig(enable_ipv6=False),
        system=system,
    )

    backend._ensure_bridge_commands(gateway_address="100.96.0.2")

    added = [c.argv for c in system.commands if "-A" in c.argv or "-I" in c.argv]
    assert backend.capabilities.ipv4_interface == "enp39s0"
    assert backend.capabilities.gateway_interface == "wg-lazycloud"
    for interface in ("enp39s0", "wg-lazycloud"):
        assert any(
            argv[2:]
            == [
                "nat",
                "-A",
                "POSTROUTING",
                "-s",
                "192.168.0.0/20",
                "-o",
                interface,
                "-j",
                "MASQUERADE",
            ]
            for argv in added
        ), interface
        assert any(
            "FORWARD" in argv and argv[-6:] == ["-i", "rt_br0", "-o", interface, "-j", "ACCEPT"]
            for argv in added
        ), interface


def test_gateway_on_the_uplink_adds_no_second_interface() -> None:
    def uplink_only(args: list[str]) -> ProcessResult:
        if args[:4] == ["ip", "-4", "route", "get"]:
            return ProcessResult(
                args=args,
                exit_code=0,
                stdout="1.2.3.4 via 10.84.1.1 dev enp39s0 src 10.84.1.103\n",
                stderr="",
            )
        return _host_with_wireguard(args)

    system = CommandNetworkSystem(run_command=uplink_only)
    capabilities = system.discover_host_capabilities(
        AgentBridgeNetworkConfig(enable_ipv6=False), gateway_address="1.2.3.4"
    )
    assert capabilities.gateway_interface == ""


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
