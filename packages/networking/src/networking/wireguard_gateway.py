from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from networking.wireguard import (
    WIREGUARD_AGENT_NETWORK,
    WIREGUARD_DEFAULT_PORT,
    WIREGUARD_GATEWAY_ADDRESS,
    WIREGUARD_INTERFACE,
    WIREGUARD_OVERLAY,
    WIREGUARD_PLATFORM_NETWORK,
    SubprocessWireGuardCommandRunner,
    WireGuardCommandRunner,
    WireGuardError,
    _ensure_interface,
    _run,
    _validate_no_overlay_route_conflict,
    derive_wireguard_public_key,
    validate_wireguard_public_key,
)

_FIREWALL_CHAIN = "LAZYCLOUD-WG"


class WireGuardGatewayPeer(Protocol):
    @property
    def public_key(self) -> str: ...

    @property
    def address(self) -> str: ...


@dataclass(slots=True)
class WireGuardGatewayRuntime:
    private_key_path: Path
    interface: str = WIREGUARD_INTERFACE
    listen_port: int = WIREGUARD_DEFAULT_PORT
    runner: WireGuardCommandRunner = field(default_factory=SubprocessWireGuardCommandRunner)

    def public_key(self) -> str:
        try:
            private_key = self.private_key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WireGuardError("WireGuard gateway private key is unreadable") from exc
        return derive_wireguard_public_key(private_key, self.runner)

    def start(self) -> None:
        if not self.private_key_path.is_file():
            raise WireGuardError("WireGuard gateway private key is unavailable")
        if self.private_key_path.stat().st_mode & 0o077:
            raise WireGuardError("WireGuard gateway private key permissions must be 0600")
        _validate_no_overlay_route_conflict(self.runner, self.interface)
        _ensure_interface(self.runner, self.interface)
        _run(
            self.runner,
            [
                "wg",
                "set",
                self.interface,
                "private-key",
                str(self.private_key_path),
                "listen-port",
                str(self.listen_port),
            ],
            "configure WireGuard gateway",
        )
        _run(
            self.runner,
            [
                "ip",
                "address",
                "replace",
                f"{WIREGUARD_GATEWAY_ADDRESS}/{WIREGUARD_PLATFORM_NETWORK.prefixlen}",
                "dev",
                self.interface,
            ],
            "assign WireGuard gateway address",
        )
        _run(self.runner, ["ip", "link", "set", "up", "dev", self.interface], "start WireGuard")
        _run(
            self.runner,
            ["ip", "route", "replace", str(WIREGUARD_OVERLAY), "dev", self.interface],
            "install WireGuard gateway route",
        )
        forwarding = _run(
            self.runner,
            ["sysctl", "-n", "net.ipv4.ip_forward"],
            "read IP forwarding configuration",
        )
        if forwarding != "1":
            raise WireGuardError("WireGuard gateway requires net.ipv4.ip_forward=1")
        self._reconcile_firewall()

    def reconcile(self, peers: Sequence[WireGuardGatewayPeer]) -> None:
        desired = {validate_wireguard_public_key(peer.public_key): peer for peer in peers}
        existing_result = self.runner.run(["wg", "show", self.interface, "allowed-ips"])
        if existing_result.returncode != 0:
            raise WireGuardError("could not read WireGuard gateway peers")
        existing: dict[str, tuple[str, ...]] = {}
        for line in existing_result.stdout.splitlines():
            public_key, separator, raw_addresses = line.partition("\t")
            if separator:
                existing[public_key] = tuple(raw_addresses.split())
        for public_key in sorted(existing.keys() - desired.keys()):
            _run(
                self.runner,
                ["wg", "set", self.interface, "peer", public_key, "remove"],
                "remove revoked WireGuard peer",
            )
        for public_key, peer in sorted(desired.items()):
            if existing.get(public_key) == (peer.address,):
                continue
            _run(
                self.runner,
                [
                    "wg",
                    "set",
                    self.interface,
                    "peer",
                    public_key,
                    "allowed-ips",
                    peer.address,
                ],
                "reconcile WireGuard peer",
            )

    def handshakes(self) -> dict[str, datetime]:
        result = self.runner.run(["wg", "show", self.interface, "latest-handshakes"])
        if result.returncode != 0:
            raise WireGuardError("could not read WireGuard gateway handshakes")
        observed: dict[str, datetime] = {}
        for line in result.stdout.splitlines():
            public_key, separator, raw_timestamp = line.partition("\t")
            if not separator:
                continue
            timestamp = int(raw_timestamp or "0")
            if timestamp > 0:
                observed[public_key] = datetime.fromtimestamp(timestamp, UTC)
        return observed

    def close(self) -> None:
        self.runner.run(["ip", "link", "delete", "dev", self.interface])

    def _reconcile_firewall(self) -> None:
        self.runner.run(["iptables", "-N", _FIREWALL_CHAIN])
        _run(self.runner, ["iptables", "-F", _FIREWALL_CHAIN], "clear WireGuard firewall")
        rules = (
            [
                "-s",
                str(WIREGUARD_PLATFORM_NETWORK),
                "-d",
                str(WIREGUARD_AGENT_NETWORK),
                "-j",
                "ACCEPT",
            ],
            [
                "-s",
                str(WIREGUARD_AGENT_NETWORK),
                "-d",
                str(WIREGUARD_PLATFORM_NETWORK),
                "-j",
                "ACCEPT",
            ],
            [
                "-s",
                str(WIREGUARD_AGENT_NETWORK),
                "-j",
                "DROP",
            ],
        )
        for rule in rules:
            _run(
                self.runner,
                ["iptables", "-A", _FIREWALL_CHAIN, *rule],
                "configure WireGuard firewall",
            )
        check = self.runner.run(
            ["iptables", "-C", "FORWARD", "-i", self.interface, "-j", _FIREWALL_CHAIN]
        )
        if check.returncode != 0:
            _run(
                self.runner,
                [
                    "iptables",
                    "-I",
                    "FORWARD",
                    "1",
                    "-i",
                    self.interface,
                    "-j",
                    _FIREWALL_CHAIN,
                ],
                "attach WireGuard firewall",
            )


__all__ = ["WireGuardGatewayPeer", "WireGuardGatewayRuntime"]
