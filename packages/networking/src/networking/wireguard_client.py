from __future__ import annotations

import fcntl
import ipaddress
import logging
import os
import shlex
import socket
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from shared.http.private_network import WireGuardGatewayConfiguration, WireGuardPeerConfiguration

from networking.wireguard import (
    WIREGUARD_GATEWAY_ADDRESS,
    WIREGUARD_GATEWAY_HEALTH_PORT,
    WIREGUARD_OVERLAY,
    SubprocessWireGuardCommandRunner,
    WireGuardCommandRunner,
    WireGuardError,
    _required_stdout,
    _run,
    _validate_no_overlay_route_conflict,
    derive_wireguard_public_key,
    generate_wireguard_private_key,
    validate_wireguard_public_key,
)

LOGGER = logging.getLogger(__name__)
_MARK_MASK = 0x00FF0000
_TABLE_BASE = 196608
_RULE_BASE = 19000
_NO_PATH = 255
_ROUTE_PROTOCOL = "4"
_INPUT_CHAIN = "LZY-WG-IN"
_OUTPUT_CHAIN = "LZY-WG-OUT"
_SELECT_CHAIN = "LZY-WG-NEW"
_PROBE_INTERVAL_SECONDS = 2.0


class _KernelOwnership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_key: str
    interfaces: set[int] = Field(default_factory=set)
    policies: dict[int, tuple[str, ...]] = Field(default_factory=dict)
    routes: set[str] = Field(default_factory=set)
    hooks: list[tuple[str, ...]] = Field(default_factory=list)
    firewall: bool = False


class _RouteKind(IntEnum):
    Unicast = 1
    Unreachable = 7


class _KernelRoute(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dst: str
    dev: str = ""
    protocol: str = ""
    gateway: str = ""
    type: _RouteKind = _RouteKind.Unicast


_ROUTES = TypeAdapter(list[_KernelRoute])


class WireGuardPathHealth(StrEnum):
    Ready = "ready"
    Draining = "draining"
    Unavailable = "unavailable"


@dataclass(slots=True)
class _GatewayPath:
    gateway: WireGuardGatewayConfiguration
    health: WireGuardPathHealth = WireGuardPathHealth.Unavailable
    refresh_after: float = 0.0

    @property
    def interface(self) -> str:
        return f"lzy-wg-{self.gateway.index}"

    @property
    def mark(self) -> int:
        return (self.gateway.index + 1) << 16


@dataclass(slots=True)
class WireGuardClientRuntime:
    state_dir: Path
    runner: WireGuardCommandRunner = field(default_factory=SubprocessWireGuardCommandRunner)
    _paths: dict[int, _GatewayPath] = field(default_factory=dict, init=False)
    _configuration: WireGuardPeerConfiguration | None = field(default=None, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _monitor: threading.Thread | None = field(default=None, init=False)
    _ownership: _KernelOwnership | None = field(default=None, init=False)
    _ownership_fd: int | None = field(default=None, init=False)
    _selection_dirty: bool = field(default=False, init=False)
    _route_deadline: float | None = field(default=None, init=False)
    _routes_expired: bool = field(default=False, init=False)

    @property
    def private_key_path(self) -> Path:
        return self.state_dir / "private-key"

    def public_key(self) -> str:
        with self._lock:
            return self._claim_ownership().public_key

    def configure(
        self,
        configuration: WireGuardPeerConfiguration,
        *,
        route_deadline: float | None = None,
    ) -> None:
        with self._lock:
            self._route_deadline = route_deadline
            if configuration == self._configuration:
                return
            owner = self._claim_ownership()
            address = ipaddress.IPv4Interface(configuration.address)
            if address.network.prefixlen != 32 or address.ip not in WIREGUARD_OVERLAY:
                raise WireGuardError("WireGuard peer requires an overlay /32 address")
            for network in configuration.allowed_ips:
                if not ipaddress.IPv4Network(network).subnet_of(WIREGUARD_OVERLAY):
                    raise WireGuardError("WireGuard routes must stay within the private overlay")
            if self._configuration is not None and (
                self._configuration.address != configuration.address
                or self._configuration.allowed_ips != configuration.allowed_ips
            ):
                raise WireGuardError(
                    "WireGuard peer address or route authority changed; rejoin required"
                )
            desired = {gateway.index: gateway for gateway in configuration.gateways}
            interfaces = tuple(f"lzy-wg-{index}" for index in owner.interfaces | desired.keys())
            _validate_no_overlay_route_conflict(self.runner, interfaces)
            key_path = self._ensure_private_key()
            for index, gateway in desired.items():
                validate_wireguard_public_key(gateway.public_key)
                current = self._paths.get(index)
                if current is not None and current.gateway == gateway:
                    continue
                path = current or _GatewayPath(gateway)
                present = self.runner.run(["ip", "link", "show", "dev", path.interface])
                if present.returncode == 0:
                    if index not in owner.interfaces:
                        raise WireGuardError(
                            f"WireGuard interface {path.interface} already has an owner"
                        )
                    self._validate_interface(index)
                else:
                    owner.interfaces.add(index)
                    self._save_ownership()
                    _run(
                        self.runner,
                        ["ip", "link", "add", "dev", path.interface, "type", "wireguard"],
                        "create WireGuard gateway path",
                    )
                self._paths[index] = path
                _run(
                    self.runner,
                    ["ip", "address", "replace", configuration.address, "dev", path.interface],
                    "assign WireGuard peer address",
                )
                peers = _run(
                    self.runner, ["wg", "show", path.interface, "peers"], "inspect WireGuard peers"
                )
                for key in peers.split():
                    if key != gateway.public_key:
                        _run(
                            self.runner,
                            ["wg", "set", path.interface, "peer", key, "remove"],
                            "retire WireGuard key",
                        )
                _run(
                    self.runner,
                    [
                        "wg",
                        "set",
                        path.interface,
                        "private-key",
                        str(key_path),
                        "peer",
                        gateway.public_key,
                        "persistent-keepalive",
                        str(configuration.persistent_keepalive_seconds),
                        "allowed-ips",
                        ",".join(configuration.allowed_ips),
                    ],
                    "configure WireGuard gateway path",
                )
                _run(
                    self.runner,
                    ["ip", "link", "set", "up", "dev", path.interface],
                    "start WireGuard gateway path",
                )
                try:
                    source_marks = Path("/proc/sys/net/ipv4/conf/all/src_valid_mark").read_text()
                    if source_marks.strip() != "1":
                        Path(f"/proc/sys/net/ipv4/conf/{path.interface}/src_valid_mark").write_text(
                            "1"
                        )
                except OSError as exc:
                    raise WireGuardError(
                        "could not enable WireGuard return-path validation"
                    ) from exc
                path.gateway = gateway
                path.health = WireGuardPathHealth.Unavailable
                self._try_refresh_endpoint(path)
                self._install_policy(index + 1, configuration.allowed_ips, path.interface)
            for index in owner.interfaces - desired.keys():
                self._remove_path(index)
            self._install_policy(_NO_PATH, configuration.allowed_ips)
            first = self._paths[min(self._paths)]
            for network in configuration.allowed_ips:
                existing = self._read_routes(["exact", network])
                if existing and network not in owner.routes:
                    raise WireGuardError(f"WireGuard source route {network} already has an owner")
                self._validate_routes(existing, (network,))
                owner.routes.add(network)
                self._save_ownership()
                _run(
                    self.runner,
                    [
                        "ip",
                        "route",
                        "replace",
                        network,
                        "dev",
                        first.interface,
                        "src",
                        str(address.ip),
                        "proto",
                        _ROUTE_PROTOCOL,
                    ],
                    "install WireGuard source route",
                )
            self._configuration = configuration
            self._replace_firewall()
            self._attach_hooks(configuration.allowed_ips)
            if self._monitor is None:
                self._stop.clear()
                self._monitor = threading.Thread(
                    target=self._watch_paths, name="wireguard-paths", daemon=True
                )
                self._monitor.start()

    def latest_handshake_at(self, server_public_key: str) -> datetime | None:
        expected = validate_wireguard_public_key(server_public_key)
        with self._lock:
            path = next((p for p in self._paths.values() if p.gateway.public_key == expected), None)
            if path is None:
                return None
            result = self.runner.run(["wg", "show", path.interface, "latest-handshakes"])
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            key, separator, raw_timestamp = line.partition("\t")
            if separator and key == expected:
                timestamp = int(raw_timestamp or "0")
                return datetime.fromtimestamp(timestamp, UTC) if timestamp > 0 else None
        return None

    def path_health(self) -> dict[int, WireGuardPathHealth]:
        with self._lock:
            return {index: path.health for index, path in self._paths.items()}

    def reconcile_connection(self, configuration: WireGuardPeerConfiguration) -> bool:
        self.configure(configuration, route_deadline=self._route_deadline)
        return WireGuardPathHealth.Ready in self.path_health().values()

    def _watch_paths(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    expired = (
                        self._route_deadline is not None and monotonic() >= self._route_deadline
                    )
                    if expired != self._routes_expired:
                        self._selection_dirty = True
                    for index, path in self._paths.items():
                        if self._stop.is_set():
                            return
                        health = self._probe(path)
                        if health is not path.health:
                            LOGGER.info("WireGuard path index=%s health=%s", index, health)
                            path.health = health
                            self._selection_dirty = True
                        if health is WireGuardPathHealth.Unavailable:
                            self._try_refresh_endpoint(path)
                    if self._selection_dirty:
                        self._replace_firewall()
            except (WireGuardError, OSError) as exc:
                LOGGER.error("WireGuard path reconciliation failed: %s", exc)
                with self._lock:
                    for path in self._paths.values():
                        path.health = WireGuardPathHealth.Unavailable
                    self._selection_dirty = True
            self._stop.wait(_PROBE_INTERVAL_SECONDS)

    @staticmethod
    def _probe(path: _GatewayPath) -> WireGuardPathHealth:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.5)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, path.mark)
                connection.connect((str(WIREGUARD_GATEWAY_ADDRESS), WIREGUARD_GATEWAY_HEALTH_PORT))
                response = bytearray()
                while len(response) <= 16 and not response.endswith(b"\n"):
                    chunk = connection.recv(16)
                    if not chunk:
                        break
                    response.extend(chunk)
                if response == b"ready\n":
                    return WireGuardPathHealth.Ready
                if response == b"draining\n":
                    return WireGuardPathHealth.Draining
        except OSError:
            pass
        return WireGuardPathHealth.Unavailable

    def _try_refresh_endpoint(self, path: _GatewayPath) -> None:
        now = monotonic()
        if now < path.refresh_after:
            return
        path.refresh_after = now + 10.0
        try:
            _required_stdout(
                self.runner.run(
                    [
                        "wg",
                        "set",
                        path.interface,
                        "peer",
                        path.gateway.public_key,
                        "endpoint",
                        path.gateway.endpoint,
                    ],
                    timeout_seconds=2.0,
                ),
                "refresh WireGuard endpoint",
                allow_empty=True,
            )
        except WireGuardError as exc:
            LOGGER.warning(
                "WireGuard endpoint refresh failed index=%s error=%s: %s",
                path.gateway.index,
                type(exc).__name__,
                exc,
            )

    def _install_policy(self, slot: int, networks: tuple[str, ...], interface: str = "") -> None:
        owner = self._claim_ownership()
        table = str(_TABLE_BASE + slot)
        priority = str(_RULE_BASE + slot)
        mark = f"{slot << 16:#x}/{_MARK_MASK:#x}"
        existing = self._policy_rule(slot)
        routes = self._read_routes(["table", table])
        if slot not in owner.policies and (existing or routes):
            raise WireGuardError(f"WireGuard routing slot {slot} already has an owner")
        self._validate_routes(routes, owner.policies.get(slot, networks), slot=slot)
        owner.policies[slot] = networks
        self._save_ownership()
        if not existing:
            _run(
                self.runner,
                ["ip", "rule", "add", "priority", priority, "fwmark", mark, "table", table],
                "install WireGuard routing policy",
            )
        for network in networks:
            route = [network, "dev", interface] if interface else ["unreachable", network]
            _run(
                self.runner,
                ["ip", "route", "replace", *route, "table", table, "proto", _ROUTE_PROTOCOL],
                "install WireGuard gateway route",
            )

    def _replace_firewall(self) -> None:
        owner = self._claim_ownership()
        mask = f"{_MARK_MASK:#x}"
        zero = f"0/{mask}"
        restore = ["-j", "CONNMARK", "--restore-mark", "--nfmask", mask, "--ctmask", mask]
        incoming = [
            [
                "-i",
                path.interface,
                "-m",
                "connmark",
                "--mark",
                zero,
                "-j",
                "CONNMARK",
                "--set-xmark",
                f"{path.mark:#x}/{mask}",
            ]
            for path in self._paths.values()
        ]
        incoming.append(restore)
        outgoing = [
            ["-m", "mark", "!", "--mark", zero, "-j", "RETURN"],
            restore,
            ["-m", "mark", "--mark", zero, "-j", _SELECT_CHAIN],
            ["-j", "CONNMARK", "--save-mark", "--nfmask", mask, "--ctmask", mask],
        ]
        configuration = self._configuration
        if configuration is None:
            raise WireGuardError("WireGuard connection selection requires a peer configuration")
        selection: list[list[str]] = []
        expired = self._route_deadline is not None and monotonic() >= self._route_deadline
        for route in () if expired else configuration.routes:
            ready = [
                self._paths[index]
                for index in route.gateway_indices
                if self._paths[index].health is WireGuardPathHealth.Ready
            ]
            for offset, path in enumerate(ready):
                remaining = len(ready) - offset
                match = (
                    ["-m", "statistic", "--mode", "nth", "--every", str(remaining), "--packet", "0"]
                    if remaining > 1
                    else []
                )
                selection.append(
                    [
                        "-d",
                        route.network,
                        "-m",
                        "mark",
                        "--mark",
                        zero,
                        *match,
                        "-j",
                        "MARK",
                        "--set-xmark",
                        f"{path.mark:#x}/{mask}",
                    ]
                )
        selection.append(
            [
                "-m",
                "mark",
                "--mark",
                zero,
                "-j",
                "MARK",
                "--set-xmark",
                f"{_NO_PATH << 16:#x}/{mask}",
            ]
        )
        chains = {_INPUT_CHAIN: incoming, _OUTPUT_CHAIN: outgoing, _SELECT_CHAIN: selection}
        self._validate_firewall()
        owner.firewall = True
        self._save_ownership()
        comment = ["-m", "comment", "--comment", self._owner_comment()]
        lines = ["*mangle"]
        lines.extend(f":{chain} - [0:0]" for chain in chains)
        for chain, rules in chains.items():
            lines.append(f"-F {chain}")
            lines.extend(" ".join(["-A", chain, *comment, *rule]) for rule in rules)
        lines.extend(("COMMIT", ""))
        _required_stdout(
            self.runner.run(
                ["iptables-restore", "--wait", "5", "--noflush"], input_text="\n".join(lines)
            ),
            "replace WireGuard connection marking",
            allow_empty=True,
        )
        self._selection_dirty = False
        self._routes_expired = expired

    def _attach_hooks(self, networks: tuple[str, ...]) -> None:
        owner = self._claim_ownership()
        comment = ("-m", "comment", "--comment", self._owner_comment())
        hooks = [("PREROUTING", "-i", "lzy-wg-+", "-j", _INPUT_CHAIN)]
        hooks.extend(("OUTPUT", "-d", network, "-j", _OUTPUT_CHAIN) for network in networks)
        for plain in hooks:
            hook = (*plain[:1], *comment, *plain[1:])
            check = self.runner.run(["iptables", "-t", "mangle", "-C", *hook])
            if check.returncode == 0:
                if hook not in owner.hooks:
                    raise WireGuardError("WireGuard firewall hook has no ownership record")
                continue
            if check.returncode != 1:
                _required_stdout(check, "inspect WireGuard firewall hook", allow_empty=True)
            if hook not in owner.hooks:
                owner.hooks.append(hook)
                self._save_ownership()
            _run(
                self.runner,
                ["iptables", "--wait", "5", "-t", "mangle", "-I", hook[0], "1", *hook[1:]],
                "attach WireGuard connection marking",
            )

    def _remove_path(self, index: int) -> None:
        owner = self._claim_ownership()
        interface = f"lzy-wg-{index}"
        if self.runner.run(["ip", "link", "show", "dev", interface]).returncode == 0:
            self._validate_interface(index)
            _run(
                self.runner,
                ["ip", "link", "delete", "dev", interface],
                "remove WireGuard gateway path",
            )
        self._paths.pop(index, None)
        self._remove_policy(index + 1)
        owner.interfaces.discard(index)
        self._save_ownership()

    def _remove_policy(self, slot: int) -> None:
        owner = self._claim_ownership()
        if slot not in owner.policies:
            return
        routes = self._read_routes(["table", str(_TABLE_BASE + slot)])
        self._validate_routes(routes, owner.policies[slot], slot=slot)
        if self._policy_rule(slot):
            _run(
                self.runner,
                [
                    "ip",
                    "rule",
                    "del",
                    "priority",
                    str(_RULE_BASE + slot),
                    "fwmark",
                    f"{slot << 16:#x}/{_MARK_MASK:#x}",
                    "table",
                    str(_TABLE_BASE + slot),
                ],
                "remove WireGuard routing policy",
            )
        for route in routes:
            target = (
                ["unreachable", route.dst] if slot == _NO_PATH else [route.dst, "dev", route.dev]
            )
            _run(
                self.runner,
                [
                    "ip",
                    "route",
                    "del",
                    *target,
                    "table",
                    str(_TABLE_BASE + slot),
                    "proto",
                    _ROUTE_PROTOCOL,
                ],
                "remove WireGuard gateway route",
            )
        del owner.policies[slot]
        self._save_ownership()

    def close(self) -> None:
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=10)
            if self._monitor.is_alive():
                raise WireGuardError("WireGuard path monitor did not stop")
            self._monitor = None
        with self._lock:
            owner = self._ownership
            if owner is None:
                return
            if owner.firewall:
                self._validate_firewall()
            for hook in tuple(owner.hooks):
                check = self.runner.run(["iptables", "-t", "mangle", "-C", *hook])
                if check.returncode == 0:
                    _run(
                        self.runner,
                        ["iptables", "--wait", "5", "-t", "mangle", "-D", *hook],
                        "detach WireGuard connection marking",
                    )
                elif check.returncode != 1:
                    _required_stdout(check, "inspect WireGuard firewall hook", allow_empty=True)
                owner.hooks.remove(hook)
                self._save_ownership()
            if owner.firewall:
                existing = self._validate_firewall()
                for chain in existing:
                    _run(
                        self.runner,
                        ["iptables", "--wait", "5", "-t", "mangle", "-F", chain],
                        "clear WireGuard connection marking",
                    )
                for chain in existing:
                    _run(
                        self.runner,
                        ["iptables", "--wait", "5", "-t", "mangle", "-X", chain],
                        "remove WireGuard connection marking",
                    )
                owner.firewall = False
                self._save_ownership()
            for network in tuple(owner.routes):
                routes = self._read_routes(["exact", network])
                self._validate_routes(routes, (network,))
                for route in routes:
                    _run(
                        self.runner,
                        [
                            "ip",
                            "route",
                            "del",
                            route.dst,
                            "dev",
                            route.dev,
                            "proto",
                            _ROUTE_PROTOCOL,
                        ],
                        "remove WireGuard source route",
                    )
                owner.routes.remove(network)
                self._save_ownership()
            for index in tuple(owner.interfaces):
                self._remove_path(index)
            for slot in tuple(owner.policies):
                self._remove_policy(slot)
            self._configuration = None
            self._ownership = None
            if self._ownership_fd is not None:
                os.close(self._ownership_fd)
                self._ownership_fd = None

    def _claim_ownership(self) -> _KernelOwnership:
        if self._ownership is not None:
            return self._ownership
        self.state_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.state_dir / "kernel.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WireGuardError(
                    "WireGuard state directory already has a running owner"
                ) from exc
            public_key = derive_wireguard_public_key(
                self._ensure_private_key().read_text(encoding="utf-8"), self.runner
            )
            journal = self.state_dir / "kernel.json"
            if journal.exists():
                if journal.stat().st_mode & 0o077:
                    raise WireGuardError("WireGuard ownership journal permissions must be 0600")
                owner = _KernelOwnership.model_validate_json(journal.read_text(encoding="utf-8"))
                if owner.public_key != public_key:
                    raise WireGuardError(
                        "WireGuard kernel ownership belongs to another private key"
                    )
                if any(index not in range(32) for index in owner.interfaces) or any(
                    slot not in (*range(1, 33), _NO_PATH) for slot in owner.policies
                ):
                    raise WireGuardError("WireGuard ownership journal contains invalid identities")
                for network in (
                    *owner.routes,
                    *(network for networks in owner.policies.values() for network in networks),
                ):
                    if not ipaddress.IPv4Network(network).subnet_of(WIREGUARD_OVERLAY):
                        raise WireGuardError("WireGuard ownership journal contains a foreign route")
                comment = ("-m", "comment", "--comment", f"lazycloud-wireguard:{public_key}")
                hooks = {
                    ("PREROUTING", *comment, "-i", "lzy-wg-+", "-j", _INPUT_CHAIN),
                    *(
                        ("OUTPUT", *comment, "-d", network, "-j", _OUTPUT_CHAIN)
                        for network in owner.routes
                    ),
                }
                if any(hook not in hooks for hook in owner.hooks) or (
                    owner.hooks and not owner.firewall
                ):
                    raise WireGuardError("WireGuard ownership journal contains a foreign hook")
            else:
                owner = _KernelOwnership(public_key=public_key)
            self._ownership = owner
            self._ownership_fd = descriptor
            self._save_ownership()
            return owner
        except BaseException:
            self._ownership = None
            self._ownership_fd = None
            os.close(descriptor)
            raise

    def _save_ownership(self) -> None:
        owner = self._ownership
        if owner is None:
            raise WireGuardError("WireGuard kernel ownership has not been acquired")
        path = self.state_dir / "kernel.json"
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(owner.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_interface(self, index: int) -> None:
        owner = self._claim_ownership()
        key = _run(
            self.runner,
            ["wg", "show", f"lzy-wg-{index}", "public-key"],
            "verify WireGuard interface ownership",
        )
        # A recorded creation can be interrupted before the private key is set.
        if index not in owner.interfaces or key not in {owner.public_key, "(none)"}:
            raise WireGuardError(f"WireGuard interface lzy-wg-{index} belongs to another key")

    def _owner_comment(self) -> str:
        return f"lazycloud-wireguard:{self._claim_ownership().public_key}"

    def _validate_firewall(self) -> tuple[str, ...]:
        owner = self._claim_ownership()
        chains = (_INPUT_CHAIN, _OUTPUT_CHAIN, _SELECT_CHAIN)
        existing = _run(
            self.runner,
            ["iptables", "-t", "mangle", "-S"],
            "inspect WireGuard connection marking",
        )
        present: set[str] = set()
        for line in existing.splitlines():
            words = shlex.split(line)
            if words[:1] == ["-N"] and words[1] in chains:
                present.add(words[1])
                if not owner.firewall:
                    raise WireGuardError("WireGuard connection marking already has an owner")
            if (
                words[:1] == ["-A"]
                and words[1] in chains
                and (
                    "--comment" not in words
                    or words[words.index("--comment") + 1] != self._owner_comment()
                )
            ):
                raise WireGuardError("WireGuard connection marking contains a foreign rule")
        return tuple(chain for chain in chains if chain in present)

    def _policy_rule(self, slot: int) -> bool:
        priority = str(_RULE_BASE + slot)
        existing = _run(
            self.runner,
            ["ip", "-N", "rule", "show", "priority", priority],
            "inspect WireGuard routing priority",
        )
        expected = [
            f"{priority}:",
            "from",
            "all",
            "fwmark",
            f"{slot << 16:#x}/{_MARK_MASK:#x}",
            "lookup",
            str(_TABLE_BASE + slot),
        ]
        if existing and existing.split() != expected:
            raise WireGuardError(f"routing priority {priority} contains a foreign rule")
        return bool(existing)

    def _read_routes(self, selector: list[str]) -> list[_KernelRoute]:
        result = self.runner.run(["ip", "-N", "-j", "route", "show", *selector])
        if result.returncode != 0 and "FIB table does not exist" in result.stderr:
            return []
        return _ROUTES.validate_json(_required_stdout(result, "inspect WireGuard routes"))

    def _validate_routes(
        self,
        routes: list[_KernelRoute],
        networks: tuple[str, ...],
        *,
        slot: int | None = None,
    ) -> None:
        owner = self._claim_ownership()
        expected = {ipaddress.IPv4Network(network) for network in networks}
        for route in routes:
            interface = (
                route.dev in {f"lzy-wg-{index}" for index in owner.interfaces}
                if slot is None
                else route.dev == ("" if slot == _NO_PATH else f"lzy-wg-{slot - 1}")
            )
            if (
                ipaddress.IPv4Network(route.dst) not in expected
                or route.protocol != _ROUTE_PROTOCOL
                or route.gateway
                or not interface
                or route.type
                is not (_RouteKind.Unreachable if slot == _NO_PATH else _RouteKind.Unicast)
            ):
                raise WireGuardError("WireGuard routing table contains a foreign route")

    def _ensure_private_key(self) -> Path:
        path = self.private_key_path
        if path.exists():
            if path.stat().st_mode & 0o077:
                raise WireGuardError("WireGuard private key permissions must be 0600")
            return path
        private_key = generate_wireguard_private_key(self.runner)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(f"{private_key}\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path
