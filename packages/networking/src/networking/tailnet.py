from __future__ import annotations

import os
import platform
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from compute.agent_control import TailnetPeerView, peer_matches_host
from pydantic import Field, JsonValue, SecretStr, TypeAdapter, field_validator
from shared.app_identity import (
    AGENT_TAILNET_DIR_NAME,
    STATE_DIR,
    TAILSCALED_SOCKET_NAME,
    TAILSCALED_STATE_NAME,
)
from shared.contracts import ContractModel

DEFAULT_TAILNET_STATE_DIR = f"{STATE_DIR}/{AGENT_TAILNET_DIR_NAME}"
DEFAULT_TAILNET_WAIT_POLL_SECONDS = 0.5
DEFAULT_TAILNET_LOGIN_TIMEOUT_SECONDS = 30.0
DEFAULT_TAILNET_STATUS_TIMEOUT_SECONDS = 5.0
DEFAULT_TAILNET_DAEMON_START_TIMEOUT_SECONDS = 10.0
TAILNET_STALE_PEER_MISS_THRESHOLD = 3
TAILNET_STALE_PEER_MISS_WINDOW_SECONDS = 60.0
TAILNET_STALE_PEER_RECOVERY_COOLDOWN_SECONDS = 300.0
TAILNET_RECONNECT_MARKER_NAME = ".reconnect-node-id"
TAILSCALED_LOG_NAME = "tailscaled.log"
SERVICE_NAME_PREFIX = "svc:"

_JSON_VALUE_ADAPTER = TypeAdapter[JsonValue](JsonValue)


class TailnetRuntimeMode(StrEnum):
    Disabled = "disabled"
    Managed = "managed"


class TailnetAuthKeyIssuer(Protocol):
    """Mints the key this runtime redeems for its own tailnet device.

    Consulted only when a login is actually required, so a restart that resumes a
    persisted device identity mints nothing.
    """

    def issue_runtime_auth_key(self, *, hostname: str, ephemeral: bool = False) -> SecretStr: ...


class TailnetRuntimeError(RuntimeError):
    pass


class TailnetAuthenticationRequired(TailnetRuntimeError):
    def __init__(self, backend_state: str) -> None:
        self.backend_state = backend_state
        state = backend_state.strip() or "unknown"
        super().__init__(f"tailnet authentication is required ({state})")


class TailnetRuntimeOptions(ContractModel):
    # Running a tailnet daemon is opted into, never inherited: a process that
    # did not ask for one must not start one as a side effect of construction.
    # The deployments that need it say so, and the remote-provider gate refuses
    # a connected deployment that left it off.
    mode: TailnetRuntimeMode = TailnetRuntimeMode.Disabled
    hostname: str = ""
    auth_key: SecretStr = SecretStr("")
    control_url: str = ""
    state_dir: str = DEFAULT_TAILNET_STATE_DIR
    socket_path: str = ""
    tailscale_binary: str = "tailscale"
    tailscaled_binary: str = "tailscaled"
    # A device that deregisters when it goes offline. Correct for anything
    # replaceable: it holds no address that was handed out, so leaving a record
    # behind on every restart buys nothing and accumulates dead peers.
    ephemeral_device: bool = False
    userspace_networking: bool = False
    login_timeout_seconds: float = Field(default=DEFAULT_TAILNET_LOGIN_TIMEOUT_SECONDS, gt=0)
    status_timeout_seconds: float = Field(default=DEFAULT_TAILNET_STATUS_TIMEOUT_SECONDS, gt=0)
    wait_poll_seconds: float = Field(default=DEFAULT_TAILNET_WAIT_POLL_SECONDS, gt=0)
    daemon_start_timeout_seconds: float = Field(
        default=DEFAULT_TAILNET_DAEMON_START_TIMEOUT_SECONDS,
        gt=0,
    )

    @field_validator("mode", mode="before")
    @classmethod
    def blank_mode_defaults_to_disabled(
        cls,
        value: str | TailnetRuntimeMode,
    ) -> str | TailnetRuntimeMode:
        if value == "":
            return TailnetRuntimeMode.Disabled
        return value


class TailnetCommandResult(ContractModel):
    returncode: int
    stdout: str = ""
    stderr: str = ""


class TailnetStatus(ContractModel):
    backend_state: str = ""
    self_node_id: str = ""
    self_host_name: str = ""
    self_dns_name: str = ""
    self_online: bool = False
    self_relay: str = ""
    tailnet_ips: list[str] = Field(default_factory=list)
    peers: list[TailnetPeerView] = Field(default_factory=list)


class TailnetReconnectMarker(ContractModel):
    node_id: str
    down_completed: bool = False


class TailnetCommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> TailnetCommandResult: ...


class TailnetManagedProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class TailnetProcessLauncher(Protocol):
    def start(self, args: list[str], *, log_path: Path | None = None) -> TailnetManagedProcess: ...


@dataclass(slots=True)
class SubprocessTailnetCommandRunner:
    def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> TailnetCommandResult:
        try:
            result = subprocess.run(
                args,
                check=False,
                capture_output=True,
                env=_merged_env(env),
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return TailnetCommandResult(
                returncode=124,
                stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or "",
                stderr=exc.stderr.decode()
                if isinstance(exc.stderr, bytes)
                else exc.stderr or str(exc),
            )
        except OSError as exc:
            return TailnetCommandResult(returncode=127, stderr=str(exc))
        return TailnetCommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


@dataclass(slots=True)
class SubprocessTailnetProcessLauncher:
    def start(self, args: list[str], *, log_path: Path | None = None) -> TailnetManagedProcess:
        # Discarding the daemon's output makes every startup failure unexplainable:
        # the machine can only report that tailscaled exited, never why, which is
        # useless on a remote machine that is already billing. Send it to a file
        # so the reason survives for the error message and for an operator.
        if log_path is None:
            return subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = log_path.open("ab")
        try:
            return subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        finally:
            stream.close()


@dataclass(slots=True)
class TailnetRuntime:
    options: TailnetRuntimeOptions
    runner: TailnetCommandRunner = field(default_factory=SubprocessTailnetCommandRunner)
    launcher: TailnetProcessLauncher = field(default_factory=SubprocessTailnetProcessLauncher)
    auth_key_issuer: TailnetAuthKeyIssuer | None = None
    _process: TailnetManagedProcess | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _peer_misses: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)
    _last_peer_recovery_at: float = field(default=0.0, init=False, repr=False)
    _peer_recovery_in_progress: bool = field(default=False, init=False, repr=False)
    _reconnect_expected_node_id: str = field(default="", init=False, repr=False)
    _reconnect_down_completed: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        with self._lock:
            if self.options.mode is TailnetRuntimeMode.Disabled:
                return
            if self._started and self._managed_process_alive():
                return
            self._validate_start_config()
            self._start_managed_daemon()
            status = self._managed_status()
            if self._resume_reconnect_if_required(status):
                return
            if not _authenticated(status):
                auth_key = self._resolve_auth_key()
                if auth_key.strip():
                    if not self.options.hostname.strip():
                        raise TailnetRuntimeError("tailnet hostname is required")
                    self._run_up(
                        auth_key=auth_key,
                        hostname=self.options.hostname,
                        control_url=self.options.control_url,
                    )
                    status = self._managed_status()
                if not _authenticated(status):
                    if status.backend_state.strip().lower() == "needslogin":
                        raise TailnetAuthenticationRequired(status.backend_state)
                    raise TailnetRuntimeError(
                        f"managed tailnet is not authenticated "
                        f"({status.backend_state or 'unknown'})"
                    )
            self._started = True

    def _resolve_auth_key(self) -> str:
        configured = self.options.auth_key.get_secret_value()
        if configured.strip() or self.auth_key_issuer is None:
            return configured
        hostname = self.options.hostname.strip()
        if not hostname:
            raise TailnetRuntimeError("tailnet hostname is required")
        try:
            return self.auth_key_issuer.issue_runtime_auth_key(
                hostname=hostname,
                ephemeral=self.options.ephemeral_device,
            ).get_secret_value()
        except TailnetRuntimeError:
            raise
        except Exception as exc:
            raise TailnetRuntimeError(f"could not mint a tailnet auth key: {exc}") from exc

    def authenticate(
        self,
        *,
        auth_key: str,
        hostname: str,
        control_url: str = "",
        force: bool = False,
    ) -> TailnetStatus:
        """Authenticate the daemon, optionally replacing an existing session.

        An already-authenticated daemon normally short-circuits, which is what
        lets a restart reuse its verified device. Set ``force`` when the control
        plane has issued a new identity: the daemon is authenticated, but under a
        name the control plane no longer recognizes, so keeping the session would
        register the wrong device.
        """
        if self.options.mode is TailnetRuntimeMode.Disabled:
            raise TailnetRuntimeError("a disabled tailnet runtime cannot authenticate")
        if not auth_key.strip():
            raise TailnetRuntimeError("tailnet auth key is not configured")
        if not hostname.strip():
            raise TailnetRuntimeError("tailnet hostname is required")
        with self._lock:
            self._validate_start_config()
            self._start_managed_daemon()
            status = self._managed_status()
            if _authenticated(status) and not force:
                self._started = True
                return status
            if force and _authenticated(status):
                # `tailscale up` will not move an authenticated node onto a new
                # identity, so the existing session has to be dropped first or
                # the daemon keeps the name the control plane already rejected.
                self.runner.run(
                    self._tailscale_args("logout"),
                    timeout_seconds=self.options.login_timeout_seconds,
                )
                self._run_up(auth_key=auth_key, hostname=hostname, control_url=control_url)
                status = self._managed_status()
                if not _authenticated(status):
                    raise TailnetRuntimeError(
                        f"tailnet re-login completed without an authenticated device "
                        f"({status.backend_state or 'unknown'})"
                    )
                self._started = True
                return status
            if status.backend_state.strip().lower() != "needslogin":
                raise TailnetRuntimeError(
                    f"tailnet cannot authenticate from state {status.backend_state or 'unknown'}"
                )
            self._run_up(auth_key=auth_key, hostname=hostname, control_url=control_url)
            status = self._managed_status()
            if not _authenticated(status):
                raise TailnetRuntimeError(
                    f"tailnet login completed without an authenticated device "
                    f"({status.backend_state or 'unknown'})"
                )
            self._started = True
            return status

    def close(self) -> None:
        """Stop the daemon, and give the device back on the way out.

        A replica is disposable: callers reach the service, never this node, so
        the device it registered has no reason to outlive the process. Logging
        out returns it immediately instead of leaving a peer the tailnet has to
        decide about later, which is how a deployment that restarts often ends up
        with a list of machines that no longer exist.

        Best effort, and deliberately so. This runs while something is already
        shutting down, and a device left behind is untidy where a shutdown that
        refuses to finish is an outage. A hard kill skips it entirely; that one
        is for a reaper to find, not for this to guarantee.
        """

        process = self._process
        self._process = None
        self._started = False
        if process is None:
            return
        with suppress(Exception):
            self.runner.run(
                self._tailscale_args("logout"),
                timeout_seconds=self.options.status_timeout_seconds,
            )
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)

    def status(self) -> TailnetStatus:
        self.start()
        result = self.runner.run(
            self._tailscale_args("status", "--json"),
            timeout_seconds=self.options.status_timeout_seconds,
        )
        if result.returncode != 0:
            raise TailnetRuntimeError(_command_error("tailscale status failed", result))
        return parse_tailscale_status(result.stdout)

    def self_dns_name(self) -> str:
        """The name this device is registered under.

        Granted by the tailnet rather than chosen here: a collision on the
        requested hostname is resolved with a numeric suffix, so what a
        deployment asked for and what it got are not reliably the same string.
        """
        if self.options.mode is TailnetRuntimeMode.Disabled:
            return ""
        return self.status().self_dns_name

    def advertise_service(self, service: str, ports: tuple[int, ...]) -> str:
        """Offer this node as a host for a service, and answer with its address.

        A service is not a device: several nodes advertise the same one and the
        tailnet routes callers to whichever is available. That is what lets the
        address outlive any single node, which a device name cannot do — ask two
        nodes for one hostname and the second is silently granted a suffixed one.

        Forwarding is raw TCP rather than TLS-terminating on purpose. The control
        plane routes one of these ports by SNI itself, and a proxy that decrypted
        on the way through would leave nothing to route on.

        A disabled tailnet answers with no address, as it does for its own device
        name, and the caller falls back to the origin it was configured with.
        """
        if self.options.mode is TailnetRuntimeMode.Disabled:
            return ""
        name = _required_service_name(service)
        for port in ports:
            result = self.runner.run(
                self._tailscale_args(
                    "serve",
                    f"--service={name}",
                    f"--tcp={port}",
                    "--bg",
                    f"tcp://127.0.0.1:{port}",
                ),
                timeout_seconds=self.options.status_timeout_seconds,
            )
            if result.returncode != 0:
                raise TailnetRuntimeError(
                    _command_error(f"advertising {name} on port {port} failed", result)
                )
        return self.service_dns_name(name)

    def service_dns_name(self, service: str) -> str:
        """Where callers reach the service, in this tailnet.

        Composed rather than read back because the two halves come from places
        that cannot disagree: the name is the one we asked for — a service that
        was already taken fails to advertise rather than quietly becoming
        something else — and the tailnet domain comes from this node's own
        registered name.
        """
        name = _required_service_name(service).removeprefix(SERVICE_NAME_PREFIX)
        _, _, domain = self.self_dns_name().strip().rstrip(".").partition(".")
        if not domain:
            raise TailnetRuntimeError("this node has no tailnet domain to place a service in")
        return f"{name}.{domain}"

    def peers(self) -> list[TailnetPeerView]:
        return self.status().peers

    def resolve_peer_host(self, host: str) -> str:
        normalized = host.strip().rstrip(".")
        if normalized == "":
            return ""
        fallback = ""
        for peer in self.status().peers:
            if not peer_matches_host(peer.host_name, peer.dns_name, normalized):
                continue
            peer_ip = next((ip for ip in peer.tailnet_ips if ip), "")
            if not peer_ip:
                continue
            if _peer_reachable(peer):
                return peer_ip
            if not fallback:
                fallback = peer_ip
        return fallback

    def wait_for_peer(self, host: str, timeout_seconds: float) -> None:
        normalized = host.strip().rstrip(".")
        if normalized == "":
            return
        deadline = time.monotonic() + max(timeout_seconds, 0.001)
        last_status = "not_found"
        unrelated_peer_reachable = False
        while True:
            peers = self.status().peers
            unrelated_peer_reachable = any(
                _peer_reachable(peer)
                and not peer_matches_host(peer.host_name, peer.dns_name, normalized)
                for peer in peers
            )
            for peer in peers:
                if not _peer_matches_target(peer, normalized):
                    continue
                if _peer_reachable(peer):
                    self._clear_peer_misses(normalized)
                    return
                last_status = "offline"
            if time.monotonic() >= deadline:
                msg = f"tailnet peer {normalized} did not become reachable ({last_status})"
                if self._record_peer_miss(
                    normalized,
                    unrelated_peer_reachable=unrelated_peer_reachable,
                ):
                    try:
                        refreshed = self._refresh_control_session()
                    except TailnetRuntimeError as exc:
                        raise TimeoutError(
                            f"{msg}; tailnet control-session refresh failed"
                        ) from exc
                    finally:
                        self._finish_peer_recovery()
                    if any(
                        peer_matches_host(peer.host_name, peer.dns_name, normalized)
                        and _peer_reachable(peer)
                        for peer in refreshed.peers
                    ):
                        self._clear_peer_misses(normalized)
                        return
                    msg = f"{msg}; tailnet control session refreshed"
                raise TimeoutError(msg)
            time.sleep(min(self.options.wait_poll_seconds, max(deadline - time.monotonic(), 0.001)))

    def _record_peer_miss(
        self,
        host: str,
        *,
        unrelated_peer_reachable: bool,
    ) -> bool:
        now = time.monotonic()
        with self._lock:
            if unrelated_peer_reachable:
                self._peer_misses.pop(host, None)
                return False
            misses = self._peer_misses.setdefault(host, [])
            misses.append(now)
            if len(misses) < TAILNET_STALE_PEER_MISS_THRESHOLD:
                return False
            if now - misses[0] < TAILNET_STALE_PEER_MISS_WINDOW_SECONDS:
                return False
            if self._peer_recovery_in_progress:
                return False
            if (
                self._last_peer_recovery_at > 0
                and now - self._last_peer_recovery_at < TAILNET_STALE_PEER_RECOVERY_COOLDOWN_SECONDS
            ):
                return False
            self._last_peer_recovery_at = now
            self._peer_recovery_in_progress = True
            misses.clear()
            return True

    def _clear_peer_misses(self, host: str) -> None:
        with self._lock:
            self._peer_misses.pop(host, None)

    def _finish_peer_recovery(self) -> None:
        with self._lock:
            self._peer_recovery_in_progress = False

    def _refresh_control_session(self) -> TailnetStatus:
        with self._lock:
            self._validate_start_config()
            before = self._managed_status()
            if not _authenticated(before) or not before.self_node_id:
                raise TailnetRuntimeError("tailnet identity is not authenticated before refresh")
            self._set_reconnect_expected_node_id(before.self_node_id, down_completed=False)
            down = self.runner.run(
                self._tailscale_args("down"),
                timeout_seconds=self.options.status_timeout_seconds,
            )
            self._started = False
            if down.returncode != 0:
                raise TailnetRuntimeError(_command_error("tailscale down failed", down))
            self._set_reconnect_expected_node_id(before.self_node_id, down_completed=True)
            return self._run_reconnect_and_verify()

    def _resume_reconnect_if_required(self, status: TailnetStatus) -> bool:
        expected_node_id = self._load_reconnect_expected_node_id()
        stopped = status.backend_state.strip().lower() == "stopped"
        if not expected_node_id and not stopped:
            return False
        if not expected_node_id:
            if not status.self_node_id:
                raise TailnetRuntimeError(
                    "stopped tailnet identity has no stable node ID; cannot reconnect safely"
                )
            expected_node_id = status.self_node_id
            self._set_reconnect_expected_node_id(expected_node_id, down_completed=True)
        if not self._reconnect_down_completed:
            if _authenticated(status):
                down = self.runner.run(
                    self._tailscale_args("down"),
                    timeout_seconds=self.options.status_timeout_seconds,
                )
                self._started = False
                if down.returncode != 0:
                    raise TailnetRuntimeError(_command_error("tailscale down failed", down))
            elif not stopped:
                raise TailnetRuntimeError(
                    f"tailnet refresh is pending from state {status.backend_state or 'unknown'}"
                )
            self._set_reconnect_expected_node_id(expected_node_id, down_completed=True)
            self._run_reconnect_and_verify()
            return True
        if _authenticated(status):
            self._complete_reconnect(status, expected_node_id=expected_node_id)
            return True
        if not stopped:
            raise TailnetRuntimeError(
                f"tailnet reconnect is pending from state {status.backend_state or 'unknown'}"
            )
        self._run_reconnect_and_verify()
        return True

    def _run_reconnect_and_verify(self) -> TailnetStatus:
        expected_node_id = self._load_reconnect_expected_node_id()
        if not expected_node_id:
            raise TailnetRuntimeError("tailnet reconnect has no expected stable node ID")
        self._run_reconnect()
        status = self._managed_status()
        self._complete_reconnect(status, expected_node_id=expected_node_id)
        return status

    def _complete_reconnect(
        self,
        status: TailnetStatus,
        *,
        expected_node_id: str,
    ) -> None:
        if not _authenticated(status):
            raise TailnetRuntimeError(
                "tailnet control-session refresh did not restore authentication"
            )
        if status.self_node_id != expected_node_id:
            raise TailnetRuntimeError("tailnet identity changed during control-session refresh")
        self._reconnect_marker_path().unlink(missing_ok=True)
        self._reconnect_expected_node_id = ""
        self._reconnect_down_completed = False
        self._started = True

    def _set_reconnect_expected_node_id(
        self,
        node_id: str,
        *,
        down_completed: bool,
    ) -> None:
        normalized = node_id.strip()
        if not normalized:
            raise TailnetRuntimeError("tailnet reconnect requires a stable node ID")
        state_dir = Path(self.options.state_dir)
        _make_private_dir(state_dir)
        marker_path = self._reconnect_marker_path()
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=state_dir,
            prefix=f"{TAILNET_RECONNECT_MARKER_NAME}.",
            delete=False,
        ) as marker:
            temporary_path = Path(marker.name)
            os.chmod(temporary_path, 0o600)
            marker.write(
                TailnetReconnectMarker(
                    node_id=normalized,
                    down_completed=down_completed,
                ).model_dump_json()
            )
            marker.write("\n")
            marker.flush()
            os.fsync(marker.fileno())
        temporary_path.replace(marker_path)
        self._reconnect_expected_node_id = normalized
        self._reconnect_down_completed = down_completed

    def _load_reconnect_expected_node_id(self) -> str:
        if self._reconnect_expected_node_id:
            return self._reconnect_expected_node_id
        try:
            marker = TailnetReconnectMarker.model_validate_json(
                self._reconnect_marker_path().read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return ""
        expected_node_id = marker.node_id.strip()
        if not expected_node_id:
            raise TailnetRuntimeError("tailnet reconnect marker has no stable node ID")
        self._reconnect_expected_node_id = expected_node_id
        self._reconnect_down_completed = marker.down_completed
        return expected_node_id

    def _reconnect_marker_path(self) -> Path:
        return Path(self.options.state_dir) / TAILNET_RECONNECT_MARKER_NAME

    def _validate_start_config(self) -> None:
        if not self.options.tailscale_binary.strip():
            raise TailnetRuntimeError("tailscale binary is not configured")
        if not self.options.tailscaled_binary.strip():
            raise TailnetRuntimeError("tailscaled binary is not configured")

    def _start_managed_daemon(self) -> None:
        if self._managed_process_alive():
            return
        state_dir = Path(self.options.state_dir)
        _make_private_dir(state_dir)
        args = [
            self.options.tailscaled_binary,
            f"--state={state_dir / TAILSCALED_STATE_NAME}",
        ]
        if self.options.userspace_networking or _managed_daemon_requires_userspace_networking():
            args.append("--tun=userspace-networking")
        socket_path = self._socket_path()
        if socket_path:
            args.append(f"--socket={socket_path}")
        log_path = state_dir / TAILSCALED_LOG_NAME
        self._process = self.launcher.start(args, log_path=log_path)
        deadline = time.monotonic() + self.options.daemon_start_timeout_seconds
        while time.monotonic() < deadline:
            if not self._managed_process_alive():
                raise TailnetRuntimeError(
                    f"managed tailscaled exited during startup: {_daemon_log_tail(log_path)}"
                )
            result = self._status_result(timeout_seconds=1.0)
            if result.returncode == 0:
                status = parse_tailscale_status(result.stdout)
                if not _managed_daemon_state_is_loading(status.backend_state):
                    return
            time.sleep(0.25)
        raise TailnetRuntimeError("managed tailscaled did not finish loading its identity state")

    def _run_up(self, *, auth_key: str, hostname: str, control_url: str) -> None:
        state_dir = Path(self.options.state_dir)
        _make_private_dir(state_dir)
        auth_key_path = _write_temp_auth_key(state_dir, auth_key)
        result = TailnetCommandResult(returncode=1, stderr="tailscale up did not run")
        deadline = time.monotonic() + self.options.login_timeout_seconds
        try:
            while True:
                args = self._tailscale_up_args(
                    auth_key_path,
                    hostname=hostname,
                    control_url=control_url,
                )
                result = self.runner.run(
                    args,
                    timeout_seconds=max(min(deadline - time.monotonic(), 5.0), 0.001),
                )
                if result.returncode == 0:
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(
                    min(self.options.wait_poll_seconds, max(deadline - time.monotonic(), 0.001))
                )
        finally:
            Path(auth_key_path).unlink(missing_ok=True)
        raise TailnetRuntimeError(
            _command_error(
                "tailscale up failed",
                result,
                secrets=(auth_key,),
            )
        )

    def _run_reconnect(self) -> None:
        result = TailnetCommandResult(returncode=1, stderr="tailscale reconnect did not run")
        deadline = time.monotonic() + self.options.login_timeout_seconds
        while True:
            result = self.runner.run(
                self._tailscale_args("up"),
                timeout_seconds=max(min(deadline - time.monotonic(), 5.0), 0.001),
            )
            if result.returncode == 0:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(min(self.options.wait_poll_seconds, max(deadline - time.monotonic(), 0.001)))
        raise TailnetRuntimeError(_command_error("tailscale reconnect failed", result))

    def _managed_status(self) -> TailnetStatus:
        result = self._status_result(timeout_seconds=self.options.status_timeout_seconds)
        if result.returncode != 0:
            raise TailnetRuntimeError(_command_error("tailscale status failed", result))
        return parse_tailscale_status(result.stdout)

    def _status_result(self, *, timeout_seconds: float) -> TailnetCommandResult:
        return self.runner.run(
            self._tailscale_args("status", "--json"),
            timeout_seconds=min(timeout_seconds, self.options.status_timeout_seconds),
        )

    def _tailscale_up_args(
        self,
        auth_key_path: str,
        *,
        hostname: str,
        control_url: str,
    ) -> list[str]:
        # Fixed, not configurable. Every node here addresses its peers by tailnet
        # name, and MagicDNS is the only thing that resolves one, so a deployment
        # that declined it would be one where the peer is reachable by address
        # and unreachable by the name actually dialled — work placed nowhere
        # while both ends report healthy. Declining it has also left a VPC
        # resolver answering `*.ts.net` from public records that point at
        # Tailscale's infrastructure instead of the peer, so every lookup
        # succeeded and every connection timed out. Non-tailnet queries are
        # forwarded upstream unchanged.
        #
        # Routes are accepted for the same reason. A service address is not a
        # peer address. The host carries it as an extra prefix among its allowed
        # IPs, and a client that declines routes drops every prefix that is not
        # the peer's own, service VIPs included. The name still resolves and the
        # policy still permits the connection, but the packet leaves by the
        # default gateway and is discarded off-tailnet, so the node pings the
        # control plane in milliseconds and cannot open a socket to the address
        # it was told to dial. Nothing in this tailnet is a subnet router, so
        # there is no foreign route to import by accepting them.
        args = self._tailscale_args(
            "up",
            f"--auth-key=file:{auth_key_path}",
            f"--hostname={hostname.strip()}",
            "--accept-dns=true",
            "--accept-routes=true",
            "--reset",
        )
        if control_url.strip():
            args.append(f"--login-server={control_url.strip()}")
        return args

    def _tailscale_args(self, *args: str) -> list[str]:
        command = [self.options.tailscale_binary]
        socket_path = self._socket_path()
        if socket_path:
            command.append(f"--socket={socket_path}")
        command.extend(args)
        return command

    def _socket_path(self) -> str:
        if self.options.socket_path:
            return self.options.socket_path
        return str(Path(self.options.state_dir) / TAILSCALED_SOCKET_NAME)

    def _managed_process_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None


def parse_tailscale_status(text: str) -> TailnetStatus:
    try:
        payload = _JSON_VALUE_ADAPTER.validate_json(text or "{}")
    except ValueError as exc:
        msg = "tailscale status returned invalid JSON"
        raise TailnetRuntimeError(msg) from exc
    if not isinstance(payload, dict):
        raise TailnetRuntimeError("tailscale status returned a non-object payload")
    self_node = _json_object(payload.get("Self"))
    peers = _json_object(payload.get("Peer"))
    return TailnetStatus(
        backend_state=_string(payload.get("BackendState")),
        self_node_id=_string(self_node.get("ID")),
        self_host_name=_string(self_node.get("HostName")),
        self_dns_name=_string(self_node.get("DNSName")),
        self_online=_bool(self_node.get("Online")),
        self_relay=_string(self_node.get("Relay")),
        tailnet_ips=_string_list(self_node.get("TailscaleIPs")),
        peers=[_peer_view(peer) for peer in peers.values() if isinstance(peer, dict)],
    )


def _peer_view(peer: dict[str, JsonValue]) -> TailnetPeerView:
    return TailnetPeerView(
        host_name=_string(peer.get("HostName")),
        dns_name=_string(peer.get("DNSName")),
        tailnet_ips=_string_list(peer.get("TailscaleIPs")),
        online=_bool(peer.get("Online")),
        active=_bool(peer.get("Active")),
        current_address=_string(peer.get("CurAddr")),
        relay=_string(peer.get("Relay")),
        last_handshake_at=_parse_timestamp(peer.get("LastHandshake")),
    )


def _write_temp_auth_key(state_dir: Path, auth_key: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=state_dir,
        prefix=".tailscale-auth-",
        delete=False,
    ) as file:
        os.chmod(file.name, 0o600)
        file.write(auth_key.strip())
        file.write("\n")
        return file.name


def _make_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _command_error(
    message: str,
    result: TailnetCommandResult,
    *,
    secrets: tuple[str, ...] = (),
) -> str:
    detail = (result.stderr or result.stdout).strip()
    if detail:
        return f"{message}: {_redact_tailnet_secret(detail, secrets=secrets)}"
    return message


def _redact_tailnet_secret(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    redacted_text = text
    for secret in secrets:
        normalized = secret.strip()
        if normalized:
            redacted_text = redacted_text.replace(normalized, "<redacted-tailnet-key>")
    words = redacted_text.split()
    redacted: list[str] = []
    for word in words:
        if "tskey-" in word:
            redacted.append("<redacted-tailnet-key>")
        else:
            redacted.append(word)
    return " ".join(redacted)


def _merged_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    merged = dict(os.environ)
    merged.update(env)
    return merged


def _required_service_name(service: str) -> str:
    normalized = service.strip()
    if not normalized:
        raise TailnetRuntimeError("a tailnet service name is required")
    if not normalized.startswith(SERVICE_NAME_PREFIX):
        return f"{SERVICE_NAME_PREFIX}{normalized}"
    if normalized == SERVICE_NAME_PREFIX:
        raise TailnetRuntimeError("a tailnet service name is required")
    return normalized


def _authenticated(status: TailnetStatus) -> bool:
    state = status.backend_state.strip().lower()
    if state in {"", "needslogin", "stopped", "nostate"}:
        return False
    return bool(status.tailnet_ips)


def _managed_daemon_state_is_loading(backend_state: str) -> bool:
    return backend_state.strip().lower() in {"", "nostate", "starting"}


def _peer_reachable(peer: TailnetPeerView) -> bool:
    return peer.online or bool(peer.current_address)


def _peer_matches_target(peer: TailnetPeerView, target: str) -> bool:
    return target in peer.tailnet_ips or peer_matches_host(peer.host_name, peer.dns_name, target)


def _daemon_log_tail(log_path: Path, *, max_lines: int = 6, max_chars: int = 600) -> str:
    """Return the end of the daemon log so a failure explains itself."""
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return f"no daemon log available ({type(exc).__name__})"
    if not content:
        return "the daemon log is empty"
    tail = " | ".join(content.splitlines()[-max_lines:])
    return tail[-max_chars:]


def _managed_daemon_requires_userspace_networking() -> bool:
    return platform.system().lower() == "darwin"


def _json_object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _string(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _bool(value: JsonValue) -> bool:
    return bool(value) if isinstance(value, bool) else False


def _parse_timestamp(value: JsonValue) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
