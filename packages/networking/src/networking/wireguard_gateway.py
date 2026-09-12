from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from threading import Event, RLock
from typing import Protocol

from networking.wireguard import (
    WIREGUARD_AGENT_NETWORK,
    WIREGUARD_DEFAULT_PORT,
    WIREGUARD_GATEWAY_ADDRESS,
    WIREGUARD_INTERFACE,
    WIREGUARD_OVERLAY,
    WIREGUARD_PLATFORM_NETWORK,
    WIREGUARD_RUNTIME_SERVICE_PORT,
    SubprocessWireGuardCommandRunner,
    WireGuardCommandRunner,
    WireGuardError,
    _ensure_interface,
    _required_stdout,
    _run,
    _validate_no_overlay_route_conflict,
    derive_wireguard_public_key,
    validate_wireguard_public_key,
)

_FIREWALL_CHAIN = "LAZYCLOUD-WG"
_RUNTIME_DNAT_CHAIN = "LAZYCLOUD-WG-DNAT"
_RUNTIME_SNAT_CHAIN = "LAZYCLOUD-WG-SNAT"
_AGENT_SOURCE_RANGE = (
    f"{IPv4Address(int(WIREGUARD_PLATFORM_NETWORK.broadcast_address) + 1)}"
    f"-{WIREGUARD_OVERLAY.broadcast_address}"
)


@dataclass(frozen=True, slots=True)
class WireGuardRuntimeService:
    address: IPv4Address
    port: int

    def __post_init__(self) -> None:
        if (
            self.address in WIREGUARD_OVERLAY
            or self.address.is_loopback
            or self.address.is_link_local
            or self.address.is_multicast
            or self.address.is_unspecified
            or self.address == IPv4Address("255.255.255.255")
        ):
            raise WireGuardError("runtime Service must resolve outside the overlay to unicast IPv4")
        if not 1 <= self.port <= 65535:
            raise WireGuardError("runtime Service port must be between 1 and 65535")


class WireGuardGatewayPeer(Protocol):
    @property
    def public_key(self) -> str: ...

    @property
    def address(self) -> str: ...

    @property
    def generation(self) -> int: ...


@dataclass(slots=True)
class WireGuardGatewayRuntime:
    private_key_path: Path
    runtime_service: WireGuardRuntimeService
    interface: str = WIREGUARD_INTERFACE
    listen_port: int = WIREGUARD_DEFAULT_PORT
    runner: WireGuardCommandRunner = field(default_factory=SubprocessWireGuardCommandRunner)
    _applied_generations: dict[str, int] = field(default_factory=dict, init=False)
    _lifecycle_lock: RLock = field(default_factory=RLock, init=False)
    _cancelled: Event = field(default_factory=Event, init=False)

    def public_key(self) -> str:
        try:
            private_key = self.private_key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WireGuardError("WireGuard gateway private key is unreadable") from exc
        return derive_wireguard_public_key(private_key, self.runner)

    def start(self, *, cancelled: Event) -> None:
        with self._lifecycle_lock:
            self._cancelled = cancelled
            self._check_active()
            try:
                self._start()
            except BaseException:
                self.close()
                raise

    def _start(self) -> None:
        if not self.private_key_path.is_file():
            raise WireGuardError("WireGuard gateway private key is unavailable")
        if self.private_key_path.stat().st_mode & 0o077:
            raise WireGuardError("WireGuard gateway private key permissions must be 0600")
        _validate_no_overlay_route_conflict(self.runner, (self.interface,))
        self._check_active()
        _ensure_interface(self.runner, self.interface)
        self._active_run(
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
        self._active_run(
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
        self._active_run(["ip", "link", "set", "up", "dev", self.interface], "start WireGuard")
        self._active_run(
            ["ip", "route", "replace", str(WIREGUARD_OVERLAY), "dev", self.interface],
            "install WireGuard gateway route",
        )
        forwarding = self._active_run(
            ["sysctl", "-n", "net.ipv4.ip_forward"],
            "read IP forwarding configuration",
        )
        if forwarding != "1":
            raise WireGuardError("WireGuard gateway requires net.ipv4.ip_forward=1")
        self._replace_filter((self.runtime_service,))
        self._replace_nat(self.runtime_service)
        self._attach_hooks()

    def reconcile(self, peers: Sequence[WireGuardGatewayPeer]) -> None:
        with self._lifecycle_lock:
            self._check_active()
            self._reconcile_peers(peers)

    def _reconcile_peers(self, peers: Sequence[WireGuardGatewayPeer]) -> None:
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
            self._check_active()
            self._active_run(
                ["wg", "set", self.interface, "peer", public_key, "remove"],
                "remove revoked WireGuard peer",
            )
            self._applied_generations.pop(public_key, None)
        for public_key, peer in sorted(desired.items()):
            self._check_active()
            if (
                existing.get(public_key) == (peer.address,)
                and self._applied_generations.get(public_key) == peer.generation
            ):
                continue
            if public_key in existing:
                self._active_run(
                    ["wg", "set", self.interface, "peer", public_key, "remove"],
                    "reset WireGuard peer generation",
                )
            self._active_run(
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
            self._applied_generations[public_key] = peer.generation

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

    def active_connections(self) -> int:
        result = _run(
            self.runner,
            ["conntrack", "-L", "--family", "ipv4", "--proto", "tcp", "--state", "ESTABLISHED"],
            "inspect WireGuard connections during drain",
        )
        active = 0
        for line in result.splitlines():
            origin: dict[str, str] = {}
            for token in line.split():
                key, separator, value = token.partition("=")
                if separator and key not in origin:
                    origin[key] = value
            source, target = origin.get("src"), origin.get("dst")
            if source is None or target is None or origin.get("dport") == "8080":
                continue
            if IPv4Address(source) in WIREGUARD_OVERLAY or IPv4Address(target) in WIREGUARD_OVERLAY:
                active += 1
        return active

    def reconcile_runtime_service(self, target: WireGuardRuntimeService) -> None:
        with self._lifecycle_lock:
            self._check_active()
            if target == self.runtime_service:
                return
            # Permit either validated target across the atomic NAT swap. Existing
            # connections retain their conntrack mapping after the old target retires.
            self._replace_filter((self.runtime_service, target))
            self._replace_nat(target)
            self._replace_filter((target,))
            self.runtime_service = target

    def close(self) -> None:
        self._cancelled.set()
        with self._lifecycle_lock:
            present = self.runner.run(["ip", "link", "show", "dev", self.interface])
            if present.returncode == 0:
                _run(
                    self.runner,
                    ["ip", "link", "delete", "dev", self.interface],
                    "remove WireGuard interface",
                )
            self._remove_firewall()
            self._applied_generations.clear()

    def _check_active(self) -> None:
        if self._cancelled.is_set():
            raise WireGuardError("WireGuard gateway ownership ended")

    def _active_run(self, args: Sequence[str], action: str, *, input_text: str = "") -> str:
        self._check_active()
        return _required_stdout(
            self.runner.run(args, input_text=input_text),
            action,
            allow_empty=True,
        )

    def _restore(self, table: str, chains: dict[str, list[list[str]]]) -> None:
        lines = [f"*{table}"]
        for name, rules in chains.items():
            lines.extend((f":{name} - [0:0]", f"-F {name}"))
            lines.extend(" ".join(("-A", name, *rule)) for rule in rules)
        lines.extend(("COMMIT", ""))
        self._active_run(
            ["iptables-restore", "--wait", "5", "--noflush"],
            f"replace WireGuard {table} rules",
            input_text="\n".join(lines),
        )

    def _runtime_origin(self) -> list[str]:
        return [
            "-m",
            "conntrack",
            "--ctstate",
            "DNAT",
            "--ctorigdst",
            str(WIREGUARD_PLATFORM_NETWORK),
            "--ctorigdstport",
            str(WIREGUARD_RUNTIME_SERVICE_PORT),
        ]

    def _replace_filter(self, targets: Sequence[WireGuardRuntimeService]) -> None:
        agent = ["-m", "iprange", "--src-range", _AGENT_SOURCE_RANGE]
        origin = self._runtime_origin()
        established = ["-m", "conntrack", "--ctstate", "ESTABLISHED"]
        rules = [
            [
                "-i",
                self.interface,
                *agent,
                "-p",
                "tcp",
                "-d",
                str(target.address),
                "--dport",
                str(target.port),
                *origin,
                "--ctdir",
                "ORIGINAL",
                "-j",
                "ACCEPT",
            ]
            for target in targets
        ]
        # An established DNAT flow was admitted against the exact Service tuple.
        # DNS refresh must not cut its response stream or broaden new-flow admission.
        rules.extend(
            (
                [
                    "-i",
                    self.interface,
                    *agent,
                    "-p",
                    "tcp",
                    *established,
                    *origin,
                    "--ctdir",
                    "ORIGINAL",
                    "-j",
                    "ACCEPT",
                ],
                [
                    "-o",
                    self.interface,
                    "-m",
                    "iprange",
                    "--dst-range",
                    _AGENT_SOURCE_RANGE,
                    "-p",
                    "tcp",
                    *established,
                    *origin,
                    "--ctdir",
                    "REPLY",
                    "-j",
                    "ACCEPT",
                ],
                [
                    "-i",
                    self.interface,
                    "-s",
                    str(WIREGUARD_PLATFORM_NETWORK),
                    "-d",
                    str(WIREGUARD_AGENT_NETWORK),
                    "-j",
                    "ACCEPT",
                ],
                [
                    "-i",
                    self.interface,
                    "-s",
                    str(WIREGUARD_AGENT_NETWORK),
                    "-d",
                    str(WIREGUARD_PLATFORM_NETWORK),
                    "-j",
                    "ACCEPT",
                ],
                ["-i", self.interface, "-s", str(WIREGUARD_AGENT_NETWORK), "-j", "DROP"],
            )
        )
        self._restore("filter", {_FIREWALL_CHAIN: rules})

    def _replace_nat(self, target: WireGuardRuntimeService) -> None:
        agent = ["-m", "iprange", "--src-range", _AGENT_SOURCE_RANGE]
        self._restore(
            "nat",
            {
                _RUNTIME_DNAT_CHAIN: [
                    [
                        "-i",
                        self.interface,
                        *agent,
                        "-d",
                        str(WIREGUARD_PLATFORM_NETWORK),
                        "-p",
                        "tcp",
                        "--dport",
                        str(WIREGUARD_RUNTIME_SERVICE_PORT),
                        "-j",
                        "DNAT",
                        "--to-destination",
                        f"{target.address}:{target.port}",
                    ]
                ],
                _RUNTIME_SNAT_CHAIN: [
                    [
                        *agent,
                        "-d",
                        str(target.address),
                        "-p",
                        "tcp",
                        "--dport",
                        str(target.port),
                        *self._runtime_origin(),
                        "--ctdir",
                        "ORIGINAL",
                        "-j",
                        "MASQUERADE",
                    ]
                ],
            },
        )

    def _hooks(self) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        return (
            ("filter", "FORWARD", ("-i", self.interface, "-j", _FIREWALL_CHAIN)),
            ("filter", "FORWARD", ("-o", self.interface, "-j", _FIREWALL_CHAIN)),
            ("nat", "PREROUTING", ("-i", self.interface, "-j", _RUNTIME_DNAT_CHAIN)),
            ("nat", "POSTROUTING", ("-j", _RUNTIME_SNAT_CHAIN)),
        )

    def _attach_hooks(self) -> None:
        for table, chain, rule in self._hooks():
            self._check_active()
            check = self.runner.run(["iptables", "-t", table, "-C", chain, *rule])
            if check.returncode == 0:
                continue
            if check.returncode != 1:
                _required_stdout(check, "inspect WireGuard firewall hook", allow_empty=True)
            self._active_run(
                ["iptables", "--wait", "5", "-t", table, "-I", chain, "1", *rule],
                "attach WireGuard firewall hook",
            )

    def _remove_firewall(self) -> None:
        present_chains = {
            table: {
                line.split()[1]
                for line in _run(
                    self.runner,
                    ["iptables", "-t", table, "-S"],
                    "inspect WireGuard firewall table",
                ).splitlines()
                if line.startswith("-N ")
            }
            for table in ("filter", "nat")
        }
        for table, chain, rule in self._hooks():
            if rule[-1] not in present_chains[table]:
                continue
            check = self.runner.run(["iptables", "-t", table, "-C", chain, *rule])
            if check.returncode == 0:
                _run(
                    self.runner,
                    ["iptables", "--wait", "5", "-t", table, "-D", chain, *rule],
                    "detach WireGuard firewall hook",
                )
            elif check.returncode != 1:
                _required_stdout(check, "inspect WireGuard firewall hook", allow_empty=True)
        for table, chain in (
            ("filter", _FIREWALL_CHAIN),
            ("nat", _RUNTIME_DNAT_CHAIN),
            ("nat", _RUNTIME_SNAT_CHAIN),
        ):
            if chain not in present_chains[table]:
                continue
            _run(
                self.runner,
                ["iptables", "--wait", "5", "-t", table, "-F", chain],
                "clear WireGuard firewall chain",
            )
            _run(
                self.runner,
                ["iptables", "--wait", "5", "-t", table, "-X", chain],
                "remove WireGuard firewall chain",
            )


__all__ = ["WireGuardGatewayPeer", "WireGuardGatewayRuntime", "WireGuardRuntimeService"]
