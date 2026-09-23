"""Local SSH setup for deployed pods: key, certificate, pinned host keys, and config.

Everything lives under the client's state home in ``ssh/``. The key never leaves
this machine; the control plane only ever sees its public half, which it signs
into a short-lived certificate for one workspace. Each workspace has its own
certificate authority, so each workspace gets its own certificate file.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex

from shared.ssh import SSH_LOGIN_USER

from lazycloud.clients.ssh.control import SshControlClient
from lazycloud.config import settings

SSH_KEEPALIVE_INTERVAL_SECONDS = 30
_PRIVATE_FILE_MODE = 0o600
_PUBLIC_FILE_MODE = 0o644
_DIRECTORY_MODE = 0o700
_ALIAS_UNSAFE = re.compile(r"[^a-z0-9-]+")
_ED25519 = b"ssh-ed25519"


class SshSetupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SshPaths:
    root: Path

    @classmethod
    def current(cls) -> SshPaths:
        return cls(root=settings().home_path / "ssh")

    @property
    def key(self) -> Path:
        return self.root / "id_ed25519"

    @property
    def public_key(self) -> Path:
        return self.root / "id_ed25519.pub"

    @property
    def known_hosts(self) -> Path:
        return self.root / "known_hosts"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def hosts(self) -> Path:
        return self.root / "hosts"

    def certificate(self, workspace: str) -> Path:
        return self.root / "certificates" / f"{_label(workspace)}-cert.pub"

    def host_config(self, alias: str) -> Path:
        return self.hosts / f"{alias}.conf"


@dataclass(frozen=True, slots=True)
class SshPodHost:
    alias: str
    pod: str
    app: str
    host_public_key: str


def ssh_host_alias(workspace: str, app: str, pod: str) -> str:
    """The host name a pod answers to in SSH config; pod names repeat across apps."""
    return f"lazycloud-{_label(workspace)}-{_label(app)}-{_label(pod)}"


@dataclass(slots=True)
class SshAccess:
    """SSH setup for one workspace, addressed by the workspace's name."""

    client: SshControlClient
    workspace: str
    paths: SshPaths
    cli_command: tuple[str, ...]
    clock: Callable[[], float] = time.time

    def ensure_key(self) -> None:
        if self.paths.key.exists() and self.paths.public_key.exists():
            return
        keygen = shutil.which("ssh-keygen")
        if keygen is None:
            raise SshSetupError("ssh-keygen is not installed; install an OpenSSH client")
        _private_directory(self.paths.root)
        self.paths.key.unlink(missing_ok=True)
        self.paths.public_key.unlink(missing_ok=True)
        subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", "lazycloud", "-f", str(self.paths.key)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        self.paths.key.chmod(_PRIVATE_FILE_MODE)

    def refresh_certificate(self, *, force: bool = False) -> bool:
        """Sign the key again when its certificate is missing, stale, or for another key."""
        self.ensure_key()
        public_key = self.paths.public_key.read_text(encoding="utf-8").strip()
        certificate = self.paths.certificate(self.workspace)
        if not force and self._certificate_current(certificate, public_key):
            return False
        response = self.client.create_certificate(public_key)
        _private_directory(certificate.parent)
        _write_atomic(certificate, response.certificate.strip() + "\n", _PUBLIC_FILE_MODE)
        return True

    def pod_host(self, pod: str, *, app: str) -> SshPodHost:
        response = self.client.host_key(pod, app=app)
        return SshPodHost(
            alias=ssh_host_alias(self.workspace, app, pod),
            pod=pod,
            app=app,
            host_public_key=response.host_public_key.strip(),
        )

    def write_hosts(self, hosts: list[SshPodHost]) -> None:
        self._refuse_alias_collisions(hosts)
        _private_directory(self.paths.root)
        _private_directory(self.paths.hosts)
        self._write_known_hosts(hosts)
        for host in hosts:
            _write_atomic(
                self.paths.host_config(host.alias), self._host_block(host), _PRIVATE_FILE_MODE
            )
        include = f"Include {_config_path(self.paths.hosts)}/*.conf\n"
        _write_atomic(self.paths.config, include, _PRIVATE_FILE_MODE)

    def _refuse_alias_collisions(self, hosts: list[SshPodHost]) -> None:
        # Labels are lowercased and hyphenated, so distinct app/pod names can
        # meet at one alias; pinning two host keys under it would break both.
        owners: dict[str, str] = {}
        for host in hosts:
            owner = self._host_owner(host)
            path = self.paths.host_config(host.alias)
            existing = path.read_text(encoding="utf-8").split("\n", 1)[0] if path.exists() else ""
            recorded = existing if existing.startswith("# lazycloud ") else None
            for claimed in (owners.get(host.alias), recorded):
                if claimed is not None and claimed != owner:
                    other = claimed.removeprefix("# lazycloud ")
                    raise SshSetupError(
                        f"SSH host {host.alias} already names another pod ({other}); "
                        "rename the app or pod so their names differ"
                    )
            owners[host.alias] = owner

    def _host_owner(self, host: SshPodHost) -> str:
        return f"# lazycloud workspace={self.workspace} app={host.app} pod={host.pod}"

    def _host_block(self, host: SshPodHost) -> str:
        proxy = shlex.join(
            [
                *self.cli_command,
                "ssh-proxy",
                host.pod,
                "--app",
                host.app,
                "--workspace",
                self.workspace,
            ]
        )
        refresh = shlex.join(
            [*self.cli_command, "ssh-cert", "--quiet", "--workspace", self.workspace]
        )
        # ssh runs the exec while reading its config, before it loads the
        # certificate, so the file it names is already fresh when it is read.
        return (
            f"{self._host_owner(host)}\n"
            f"Match originalhost {host.alias} exec {_quoted_exec(refresh)}\n"
            f"\n"
            f"Host {host.alias}\n"
            f"    HostName {host.alias}\n"
            f"    HostKeyAlias {host.alias}\n"
            f"    User {SSH_LOGIN_USER}\n"
            f"    ProxyCommand {proxy}\n"
            f"    IdentityFile {_config_path(self.paths.key)}\n"
            f"    CertificateFile {_config_path(self.paths.certificate(self.workspace))}\n"
            f"    IdentitiesOnly yes\n"
            f"    UserKnownHostsFile {_config_path(self.paths.known_hosts)}\n"
            f"    StrictHostKeyChecking yes\n"
            f"    ServerAliveInterval {SSH_KEEPALIVE_INTERVAL_SECONDS}\n"
        )

    def _write_known_hosts(self, hosts: list[SshPodHost]) -> None:
        aliases = {host.alias for host in hosts}
        existing = (
            self.paths.known_hosts.read_text(encoding="utf-8").splitlines()
            if self.paths.known_hosts.exists()
            else []
        )
        kept = [line for line in existing if line.split(" ", 1)[0] not in aliases]
        pinned = [f"{host.alias} {_key_type_and_blob(host.host_public_key)}" for host in hosts]
        _write_atomic(self.paths.known_hosts, "\n".join([*kept, *pinned]) + "\n", _PUBLIC_FILE_MODE)

    def _certificate_current(self, certificate: Path, public_key: str) -> bool:
        try:
            line = certificate.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        try:
            certified_key, valid_after, valid_before = _certificate_facts(line)
        except (ValueError, IndexError, struct.error):
            return False
        if certified_key != _ed25519_public_bytes(public_key):
            return False
        return self.clock() < valid_after + (valid_before - valid_after) / 2


def install_ssh_include(paths: SshPaths, *, user_config: Path | None = None) -> bool:
    """Name the LazyCloud config at the top of the user's OpenSSH config once.

    Returns whether the user's config changed. Nothing else in it is touched.
    """
    target = user_config or Path.home() / ".ssh" / "config"
    include = f"Include {_config_path(paths.config)}"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    for line in existing.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0].lower() == "include" and include.split()[1] in words[1:]:
            return False
    _private_directory(target.parent)
    mode = target.stat().st_mode & 0o777 if target.exists() else _PRIVATE_FILE_MODE
    _write_atomic(target, f"{include}\n\n{existing}" if existing else f"{include}\n", mode)
    return True


def run_ssh(config: Path, alias: str, arguments: list[str]) -> int:
    ssh = shutil.which("ssh")
    if ssh is None:
        raise SshSetupError("ssh is not installed; install an OpenSSH client")
    return subprocess.call([ssh, "-F", str(config), *arguments, alias])


def bridge_stdio(url: str, *, token: str) -> int:
    """Carry SSH bytes between stdin/stdout and the pod tunnel until either side ends.

    Stdout carries the SSH stream and nothing else; every diagnostic goes to stderr.
    """
    from websockets.exceptions import ConnectionClosed, InvalidHandshake
    from websockets.sync.client import connect

    stdin = sys.stdin.buffer.fileno()
    stdout = sys.stdout.buffer.fileno()
    try:
        websocket = connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=30,
            close_timeout=5,
            max_size=None,
            compression=None,
            # SSH keepalives cover the idle stream.
            ping_interval=None,
        )
    except (OSError, InvalidHandshake) as exc:
        print(f"lazycloud: could not open the SSH tunnel: {exc}", file=sys.stderr)
        return 255

    def forward_input() -> None:
        try:
            while data := os.read(stdin, 64 * 1024):
                websocket.send(data)
        except (OSError, ConnectionClosed):
            pass
        websocket.close()

    threading.Thread(target=forward_input, daemon=True).start()
    try:
        while True:
            message = websocket.recv()
            payload = message if isinstance(message, bytes) else message.encode("utf-8")
            view = memoryview(payload)
            while view:
                view = view[os.write(stdout, view) :]
    except ConnectionClosed as closed:
        received = closed.rcvd
        if received is not None and received.code != 1000:
            print(
                f"lazycloud: SSH tunnel closed: {received.reason or received.code}", file=sys.stderr
            )
            return 255
        return 0
    except OSError:
        websocket.close()
        return 0


def current_cli_command() -> tuple[str, ...]:
    """How ssh should invoke this CLI, independent of the PATH it runs with."""
    executable = shutil.which("lazycloud")
    if executable is not None:
        return (str(Path(executable).resolve()),)
    return (sys.executable, "-m", "lazycloud.cli.main")


def _certificate_facts(line: str) -> tuple[bytes, int, int]:
    reader = _SshReader(base64.b64decode(line.split()[1]))
    reader.string()
    reader.string()
    certified_key = reader.string()
    reader.uint64()
    reader.uint32()
    reader.string()
    reader.string()
    return certified_key, reader.uint64(), reader.uint64()


def _ed25519_public_bytes(line: str) -> bytes:
    reader = _SshReader(base64.b64decode(line.split()[1]))
    if reader.string() != _ED25519:
        raise SshSetupError("the LazyCloud SSH key is not an ed25519 key")
    return reader.string()


def _key_type_and_blob(line: str) -> str:
    words = line.split()
    if len(words) < 2:
        raise SshSetupError("the control plane returned an invalid host key")
    return f"{words[0]} {words[1]}"


class _SshReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def string(self) -> bytes:
        length = self.uint32()
        value = self._data[self._offset : self._offset + length]
        if len(value) != length:
            raise ValueError("truncated SSH field")
        self._offset += length
        return value

    def uint32(self) -> int:
        (value,) = struct.unpack_from(">I", self._data, self._offset)
        self._offset += 4
        return int(value)

    def uint64(self) -> int:
        (value,) = struct.unpack_from(">Q", self._data, self._offset)
        self._offset += 8
        return int(value)


def _label(value: str) -> str:
    label = _ALIAS_UNSAFE.sub("-", value.strip().lower()).strip("-")
    if not label:
        raise SshSetupError(f"cannot build an SSH host name from {value!r}")
    return label


def _config_path(path: Path) -> str:
    text = str(path)
    if any(character.isspace() or character == '"' for character in text):
        return f'"{text}"'
    return text


def _quoted_exec(command: str) -> str:
    if '"' in command:
        raise SshSetupError("the lazycloud executable path cannot contain a double quote")
    return f'"{command}"'


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)


def _write_atomic(path: Path, content: str, mode: int) -> None:
    staged = path.with_name(f".{path.name}.{os.getpid()}.{token_hex(4)}")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(content.encode("utf-8"))
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(staged, path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


__all__ = [
    "SSH_KEEPALIVE_INTERVAL_SECONDS",
    "SshAccess",
    "SshPaths",
    "SshPodHost",
    "SshSetupError",
    "bridge_stdio",
    "current_cli_command",
    "install_ssh_include",
    "run_ssh",
    "ssh_host_alias",
]
