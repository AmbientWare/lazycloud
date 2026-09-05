from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import os
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

WIREGUARD_INTERFACE = "wg-lazycloud"
WIREGUARD_OVERLAY = ipaddress.IPv4Network("100.96.0.0/11")
WIREGUARD_PLATFORM_NETWORK = ipaddress.IPv4Network("100.96.0.0/24")
WIREGUARD_AGENT_NETWORK = WIREGUARD_OVERLAY
WIREGUARD_GATEWAY_ADDRESS = ipaddress.IPv4Address("100.96.0.1")
WIREGUARD_KEEPALIVE_SECONDS = 25
WIREGUARD_DEFAULT_PORT = 51820
WIREGUARD_GATEWAY_HEALTH_PORT = 8080
WIREGUARD_RUNTIME_SERVICE_PORT = 9000
WIREGUARD_AGENT_ROUTE_PROXY_PORT = 29443
WIREGUARD_PLATFORM_PEER_LIMIT = 32
_AGENT_FIRST_ADDRESS = int(ipaddress.IPv4Address("100.96.1.1"))
_AGENT_LAST_ADDRESS = int(WIREGUARD_OVERLAY.broadcast_address) - 1
LOGGER = logging.getLogger(__name__)


class WireGuardError(RuntimeError):
    pass


class WireGuardCommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    returncode: int
    stdout: str = ""
    stderr: str = ""


class WireGuardCommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        input_text: str = "",
        timeout_seconds: float = 10.0,
    ) -> WireGuardCommandResult: ...


@dataclass(slots=True)
class SubprocessWireGuardCommandRunner:
    env: Mapping[str, str] = field(default_factory=dict[str, str])

    def run(
        self,
        args: Sequence[str],
        *,
        input_text: str = "",
        timeout_seconds: float = 10.0,
    ) -> WireGuardCommandResult:
        try:
            completed = subprocess.run(
                list(args),
                check=False,
                capture_output=True,
                text=True,
                input=input_text or None,
                timeout=timeout_seconds,
                env={**os.environ, **self.env},
            )
        except subprocess.TimeoutExpired as exc:
            raise WireGuardError(f"command timed out: {args[0]}") from exc
        except OSError as exc:
            raise WireGuardError(f"could not run {args[0]}: {exc}") from exc
        return WireGuardCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class WireGuardPeerConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    peer_id: str = Field(min_length=1)
    address: str
    server_public_key: str = Field(min_length=44, max_length=44)
    endpoint: str = Field(min_length=3)
    allowed_ips: tuple[str, ...]
    persistent_keepalive_seconds: int = Field(default=WIREGUARD_KEEPALIVE_SECONDS, ge=1, le=120)
    generation: int = Field(ge=1)


class _IpRoute(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    dev: str = ""
    dst: str = ""


def validate_wireguard_public_key(value: str) -> str:
    normalized = value.strip()
    if len(normalized) != 44:
        raise ValueError("WireGuard public key must be 44 base64 characters")
    try:
        decoded = base64.b64decode(normalized, validate=True)
    except ValueError as exc:
        raise ValueError("WireGuard public key must be valid base64") from exc
    if len(decoded) != 32:
        raise ValueError("WireGuard public key must encode 32 bytes")
    return normalized


def generate_wireguard_private_key(
    runner: WireGuardCommandRunner | None = None,
) -> str:
    command_runner = runner or SubprocessWireGuardCommandRunner()
    return _required_stdout(command_runner.run(["wg", "genkey"]), "generate WireGuard key")


def derive_wireguard_public_key(
    private_key: str,
    runner: WireGuardCommandRunner | None = None,
) -> str:
    command_runner = runner or SubprocessWireGuardCommandRunner()
    result = command_runner.run(["wg", "pubkey"], input_text=f"{private_key.strip()}\n")
    return validate_wireguard_public_key(_required_stdout(result, "derive WireGuard public key"))


def allocate_wireguard_agent_address(
    seed: str,
    *,
    is_allocated: Callable[[str], bool],
) -> str:
    if not seed:
        raise ValueError("WireGuard address allocation seed is required")
    capacity = _AGENT_LAST_ADDRESS - _AGENT_FIRST_ADDRESS + 1
    start = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big") % capacity
    for offset in range(capacity):
        raw = _AGENT_FIRST_ADDRESS + ((start + offset) % capacity)
        candidate = f"{ipaddress.IPv4Address(raw)}/32"
        if not is_allocated(candidate):
            return candidate
    raise WireGuardError("WireGuard agent address pool is exhausted")


def wireguard_platform_index(
    configured_index: int | None,
    *,
    pod_name: str = "",
) -> int:
    if pod_name.strip():
        name = pod_name.strip()
        _, separator, raw_index = name.rpartition("-")
        if not separator or not raw_index.isdigit():
            raise ValueError(f"WireGuard platform pod name has no ordinal: {name!r}")
        index = int(raw_index)
    elif configured_index is not None:
        index = configured_index
    else:
        raise ValueError("WireGuard platform index or pod name is required")
    if not 0 <= index < WIREGUARD_PLATFORM_PEER_LIMIT:
        raise ValueError(f"WireGuard platform index must be below {WIREGUARD_PLATFORM_PEER_LIMIT}")
    return index


def wireguard_platform_address(index: int) -> ipaddress.IPv4Address:
    checked_index = wireguard_platform_index(index)
    return ipaddress.IPv4Address(
        int(WIREGUARD_PLATFORM_NETWORK.network_address) + checked_index + 2
    )


@dataclass(slots=True)
class WireGuardClientRuntime:
    state_dir: Path
    interface: str = WIREGUARD_INTERFACE
    runner: WireGuardCommandRunner = field(default_factory=SubprocessWireGuardCommandRunner)

    @property
    def private_key_path(self) -> Path:
        return self.state_dir / "private-key"

    def public_key(self) -> str:
        return derive_wireguard_public_key(self._private_key(), self.runner)

    def configure(self, configuration: WireGuardPeerConfiguration) -> None:
        _validate_no_overlay_route_conflict(self.runner, self.interface)
        private_key_path = self._ensure_private_key()
        _ensure_interface(self.runner, self.interface)
        _run(
            self.runner,
            [
                "wg",
                "set",
                self.interface,
                "peer",
                configuration.server_public_key,
                "remove",
            ],
            "reset WireGuard server peer",
        )
        _run(
            self.runner,
            ["ip", "address", "replace", configuration.address, "dev", self.interface],
            "assign WireGuard address",
        )
        command = [
            "wg",
            "set",
            self.interface,
            "private-key",
            str(private_key_path),
            "peer",
            configuration.server_public_key,
            "endpoint",
            configuration.endpoint,
            "persistent-keepalive",
            str(configuration.persistent_keepalive_seconds),
            "allowed-ips",
            ",".join(configuration.allowed_ips),
        ]
        _run(self.runner, command, "configure WireGuard peer")
        _run(self.runner, ["ip", "link", "set", "up", "dev", self.interface], "start WireGuard")
        for network in configuration.allowed_ips:
            _run(
                self.runner,
                ["ip", "route", "replace", network, "dev", self.interface],
                "install WireGuard route",
            )

    def latest_handshake_at(self, server_public_key: str) -> datetime | None:
        result = self.runner.run(["wg", "show", self.interface, "latest-handshakes"])
        if result.returncode != 0:
            return None
        expected = validate_wireguard_public_key(server_public_key)
        for line in result.stdout.splitlines():
            public_key, separator, raw_timestamp = line.partition("\t")
            if separator and public_key == expected:
                timestamp = int(raw_timestamp or "0")
                return datetime.fromtimestamp(timestamp, UTC) if timestamp > 0 else None
        return None

    def reconcile_connection(self, configuration: WireGuardPeerConfiguration) -> bool:
        try:
            with socket.create_connection(
                (str(WIREGUARD_GATEWAY_ADDRESS), WIREGUARD_GATEWAY_HEALTH_PORT), timeout=1.0
            ):
                return True
        except OSError:
            LOGGER.warning("WireGuard gateway is unreachable; refreshing peer endpoint DNS")
        try:
            _required_stdout(
                self.runner.run(
                    [
                        "wg",
                        "set",
                        self.interface,
                        "peer",
                        configuration.server_public_key,
                        "endpoint",
                        configuration.endpoint,
                    ],
                    timeout_seconds=2.0,
                ),
                "refresh WireGuard peer endpoint",
                allow_empty=True,
            )
        except WireGuardError:
            LOGGER.exception("WireGuard endpoint refresh failed; retaining configured peer")
        return False

    def close(self) -> None:
        self.runner.run(["ip", "link", "delete", "dev", self.interface])

    def _private_key(self) -> str:
        path = self._ensure_private_key()
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WireGuardError("WireGuard private key is unreadable") from exc

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


def _validate_no_overlay_route_conflict(
    runner: WireGuardCommandRunner,
    interface: str,
) -> None:
    result = runner.run(["ip", "-j", "route", "show"])
    parsed_routes = TypeAdapter(list[_IpRoute]).validate_json(
        _required_stdout(result, "inspect host routes") or "[]"
    )
    for route in parsed_routes:
        if route.dev == interface:
            continue
        destination = route.dst
        if not destination or destination == "default":
            continue
        try:
            network = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            continue
        if network.overlaps(WIREGUARD_OVERLAY):
            raise WireGuardError(
                f"existing route {destination} overlaps WireGuard overlay {WIREGUARD_OVERLAY}"
            )


def _ensure_interface(runner: WireGuardCommandRunner, interface: str) -> None:
    result = runner.run(["ip", "link", "show", "dev", interface])
    if result.returncode == 0:
        return
    _run(
        runner,
        ["ip", "link", "add", "dev", interface, "type", "wireguard"],
        "create WireGuard interface",
    )


def _run(runner: WireGuardCommandRunner, args: Sequence[str], action: str) -> str:
    return _required_stdout(runner.run(args), action, allow_empty=True)


def _required_stdout(
    result: WireGuardCommandResult,
    action: str,
    *,
    allow_empty: bool = False,
) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise WireGuardError(f"could not {action}: {detail}")
    value = result.stdout.strip()
    if not value and not allow_empty:
        raise WireGuardError(f"could not {action}: command returned no data")
    return value


__all__ = [
    "WIREGUARD_AGENT_NETWORK",
    "WIREGUARD_DEFAULT_PORT",
    "WIREGUARD_GATEWAY_ADDRESS",
    "WIREGUARD_GATEWAY_HEALTH_PORT",
    "WIREGUARD_INTERFACE",
    "WIREGUARD_KEEPALIVE_SECONDS",
    "WIREGUARD_OVERLAY",
    "WIREGUARD_PLATFORM_NETWORK",
    "WIREGUARD_PLATFORM_PEER_LIMIT",
    "WIREGUARD_RUNTIME_SERVICE_PORT",
    "SubprocessWireGuardCommandRunner",
    "WireGuardClientRuntime",
    "WireGuardCommandResult",
    "WireGuardCommandRunner",
    "WireGuardError",
    "WireGuardPeerConfiguration",
    "allocate_wireguard_agent_address",
    "derive_wireguard_public_key",
    "generate_wireguard_private_key",
    "validate_wireguard_public_key",
    "wireguard_platform_address",
    "wireguard_platform_index",
]
