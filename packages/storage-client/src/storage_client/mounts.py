from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from foundation.process import (
    ManagedCommandResult,
    ManagedCommandStillRunning,
    ProcessResult,
    run_command_with_timeout,
    start_managed_command,
)
from pydantic import Field
from shared.contracts import ContractModel

LOGGER = logging.getLogger(__name__)

DEFAULT_MOUNT_TIMEOUT_SECONDS = 30.0
DEFAULT_UNMOUNT_TIMEOUT_SECONDS = 10.0
DEFAULT_MOUNT_POLL_SECONDS = 0.1
DEFAULT_CLEANUP_RETRIES = 3


class StorageMountMode(StrEnum):
    GeeseFs = "geesefs"
    MountPoint = "mountpoint"


class StorageMountStatus(StrEnum):
    Mounted = "mounted"
    AlreadyMounted = "already-mounted"
    Unmounted = "unmounted"
    AlreadyUnmounted = "already-unmounted"
    Failed = "failed"


class StorageMountResult(ContractModel):
    mode: StorageMountMode
    local_path: str
    status: StorageMountStatus
    command: list[str] = Field(default_factory=list)
    env_keys: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    output: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {
            StorageMountStatus.Mounted,
            StorageMountStatus.AlreadyMounted,
            StorageMountStatus.Unmounted,
            StorageMountStatus.AlreadyUnmounted,
        }


# 32MB, matching the readahead a production geesefs deployment settles on: large
# enough for linear reads, small enough that concurrent readers stay inside the
# memory limit.
DEFAULT_GEESEFS_READ_AHEAD_LARGE_KB = 32 * 1024
GEESEFS_MIN_MEMORY_LIMIT_MB = 128


def geesefs_memory_limit_mb(*, configured_mb: int, worker_memory_mib: int) -> int:
    """Bound the mount's data cache by the worker it runs on.

    A fixed limit claims the same RAM on every worker, which on a small one is
    most of the machine. Half the worker's memory, floored so a tiny worker
    still gets a usable cache, and never above what was configured.
    """
    if worker_memory_mib <= 0:
        return configured_mb
    ceiling = max(worker_memory_mib // 2, GEESEFS_MIN_MEMORY_LIMIT_MB)
    ceiling = min(ceiling, worker_memory_mib)
    if configured_mb <= 0:
        return ceiling
    return min(configured_mb, ceiling)


class GeeseFsMountConfig(ContractModel):
    """A workspace's own bucket, mounted so files map one-to-one onto S3 keys.

    That mapping is the whole point: the object a container writes through the
    mount is the same object the control plane presigns for a client read.
    """

    bucket_name: str
    prefix: str = ""
    endpoint_url: str = Field(min_length=1)
    region: str = ""
    access_key: str = ""
    secret_key: str = ""
    session_token: str = ""
    shared_config_path: str = ""
    """An AWS shared-config file whose `workspace` profile names a credential source.

    Preferred over the key fields above for a mount that has to outlive its
    credential: the SDK re-reads the profile when the cached credential nears
    expiry, where the environment is read once at start and cannot be revised.
    """
    force_path_style: bool = True
    cache_dir: str = ""
    memory_limit_mb: int = 1024
    max_flushers: int = 16
    stat_cache_ttl_seconds: int = 1
    # GeeseFS defaults large readahead to 100MB, and it allocates that per
    # concurrent reader, so a handful of parallel large reads overruns
    # memory_limit_mb and makes the limit advisory rather than real.
    read_ahead_large_kb: int = DEFAULT_GEESEFS_READ_AHEAD_LARGE_KB
    # Make the limit refuse work instead of exceeding it.
    enforce_memory_limit: bool = True
    preload_directories: bool = False
    dir_mode: str = "0777"
    file_mode: str = "0666"
    binary: str = "geesefs"

    @property
    def mount_target(self) -> str:
        prefix = self.prefix.strip("/")
        return f"{self.bucket_name}:{prefix}" if prefix else self.bucket_name


class MountPointConfig(ContractModel):
    bucket_name: str
    prefix: str = ""
    access_key: str = ""
    secret_key: str = ""
    endpoint_url: str = ""
    region: str = ""
    read_only: bool = False
    force_path_style: bool = False
    binary: str = "ms3"
    log_directory: str = "/var/log/"


type MountChecker = Callable[[str], bool]


class StorageManagedCommand(Protocol):
    def poll(self) -> ManagedCommandResult | None: ...

    def output(self) -> str: ...

    def terminate(self, *, timeout_seconds: float = 5) -> ManagedCommandResult: ...


type ManagedCommandStarter = Callable[[list[str], dict[str, str] | None], StorageManagedCommand]
type CommandRunner = Callable[[float, list[str]], ProcessResult]


@dataclass(slots=True)
class StorageMountSystem:
    mount_checker: MountChecker = lambda path: is_mounted(path)
    start_command: ManagedCommandStarter = lambda argv, env: start_managed_command(argv, env=env)
    run_command: CommandRunner = lambda timeout, argv: run_command_with_timeout(timeout, argv)


def _terminate_managed_mount(command: StorageManagedCommand | None) -> None:
    if command is None:
        return
    try:
        result = command.terminate(timeout_seconds=DEFAULT_UNMOUNT_TIMEOUT_SECONDS)
    except ManagedCommandStillRunning:
        LOGGER.warning("storage mount process did not exit before the unmount timeout")
        return
    if result.exit_code not in {0, None}:
        LOGGER.warning(
            "storage mount process exited %s: %s", result.exit_code, result.output.strip()
        )


class StorageMountManager:
    mode: StorageMountMode

    def mount(self, local_path: str) -> StorageMountResult:
        raise NotImplementedError

    def unmount(self, local_path: str) -> StorageMountResult:
        raise NotImplementedError


@dataclass(slots=True)
class GeeseFsMountManager(StorageMountManager):
    config: GeeseFsMountConfig
    system: StorageMountSystem = field(default_factory=StorageMountSystem)
    mount_cmd: StorageManagedCommand | None = None
    mode: StorageMountMode = StorageMountMode.GeeseFs

    def mount(self, local_path: str) -> StorageMountResult:
        unresolved = unresolvable_endpoint_host(self.config.endpoint_url)
        if unresolved:
            return _status_result(
                self.mode,
                local_path,
                StorageMountStatus.Failed,
                reason=(
                    f"storage mount endpoint host {unresolved!r} does not resolve from this worker"
                ),
            )
        Path(local_path).mkdir(parents=True, exist_ok=True)
        if self.system.mount_checker(local_path):
            return _status_result(self.mode, local_path, StorageMountStatus.AlreadyMounted)
        # A mount torn down moments earlier can leave a dead FUSE entry behind.
        # Mounting over one times out, so clear it first; this is a no-op when
        # the path is genuinely free.
        retry_force_unmount(local_path, self.system, mode=self.mode)
        if self.config.cache_dir:
            # geesefs will not create its own cache directory and stalls
            # without one, which surfaces only as a mount timeout.
            Path(self.config.cache_dir).mkdir(parents=True, exist_ok=True)
        command = geesefs_command(self.config, local_path)
        env = self._env()
        self.mount_cmd = self.system.start_command(command, env or None)
        mounted = _wait_for_mount(
            local_path,
            self.system.mount_checker,
            self.mount_cmd,
            mode=self.mode,
            timeout_seconds=DEFAULT_MOUNT_TIMEOUT_SECONDS,
        )
        if mounted.ok:
            LOGGER.info("geesefs mounted %s at %s", self.config.mount_target, local_path)
            return _status_result(self.mode, local_path, StorageMountStatus.Mounted, command, env)
        output = mounted.output
        LOGGER.warning(
            "geesefs mount failed for %s (%s): %s\n%s",
            local_path,
            self.config.mount_target,
            mounted.reason,
            output.strip() or "<no output from geesefs>",
        )
        self._terminate_mount_cmd()
        return StorageMountResult(
            mode=self.mode,
            local_path=local_path,
            status=StorageMountStatus.Failed,
            command=command,
            env_keys=sorted(env),
            output=output,
            reason=mounted.reason,
        )

    def unmount(self, local_path: str) -> StorageMountResult:
        result = retry_force_unmount(local_path, self.system, mode=self.mode)
        self._terminate_mount_cmd()
        if not result.ok:
            LOGGER.warning("geesefs unmount of %s failed: %s", local_path, result.reason)
        return result.model_copy(update={"mode": self.mode})

    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.config.access_key:
            env["AWS_ACCESS_KEY_ID"] = self.config.access_key
        if self.config.secret_key:
            env["AWS_SECRET_ACCESS_KEY"] = self.config.secret_key
        if self.config.session_token:
            env["AWS_SESSION_TOKEN"] = self.config.session_token
        return env

    def _terminate_mount_cmd(self) -> None:
        _terminate_managed_mount(self.mount_cmd)
        self.mount_cmd = None


def geesefs_command(config: GeeseFsMountConfig, local_path: str) -> list[str]:
    command = [
        config.binary,
        "-f",
        "-o",
        "allow_other",
        # GeeseFS stores no permissions, so a narrower mode would deny every
        # container that does not happen to run as the mounting uid.
        f"--dir-mode={config.dir_mode}",
        f"--file-mode={config.file_mode}",
        "--uid=0",
        "--gid=0",
        # Correctness, not tuning: a container write must be durable before the
        # client presigns a read of the same object.
        "--fsync-on-close",
        # The inverse direction: a client write through the API must be visible
        # to the mount promptly rather than after the default one-minute TTL.
        f"--stat-cache-ttl={config.stat_cache_ttl_seconds}s",
        f"--memory-limit={config.memory_limit_mb}",
        f"--max-flushers={config.max_flushers}",
        f"--read-ahead-large={config.read_ahead_large_kb}",
    ]
    if config.enforce_memory_limit:
        command.append("--use-enomem")
    if not config.preload_directories:
        # Listing a large bucket up front costs metadata cache for entries no
        # container asked for.
        command.append("--no-preload-dir")
    command.append(f"--endpoint={config.endpoint_url}")
    if config.region:
        command.append(f"--region={config.region}")
    if not config.force_path_style:
        command.append("--subdomain")
    if config.cache_dir:
        command.append(f"--cache={config.cache_dir}")
    if config.shared_config_path:
        command.extend([f"--shared-config={config.shared_config_path}", "--profile=workspace"])
    command.extend([config.mount_target, local_path])
    return command


@dataclass(slots=True)
class MountPointMountManager(StorageMountManager):
    config: MountPointConfig
    system: StorageMountSystem = field(default_factory=StorageMountSystem)
    mount_cmd: StorageManagedCommand | None = None
    mode: StorageMountMode = StorageMountMode.MountPoint

    def mount(self, local_path: str) -> StorageMountResult:
        unresolved = unresolvable_endpoint_host(self.config.endpoint_url)
        if unresolved:
            return _status_result(
                self.mode,
                local_path,
                StorageMountStatus.Failed,
                reason=(
                    f"storage mount endpoint host {unresolved!r} does not resolve from this worker"
                ),
            )
        self.unmount(local_path)
        Path(local_path).mkdir(parents=True, exist_ok=True)
        command = mountpoint_command(self.config, local_path)
        env = mountpoint_env(self.config)
        self.mount_cmd = self.system.start_command(command, env or None)
        mounted = _wait_for_mount(
            local_path,
            self.system.mount_checker,
            self.mount_cmd,
            mode=self.mode,
            timeout_seconds=DEFAULT_MOUNT_TIMEOUT_SECONDS,
        )
        if mounted.ok:
            return _status_result(self.mode, local_path, StorageMountStatus.Mounted, command, env)
        output = mounted.output
        self._terminate_mount_cmd()
        return StorageMountResult(
            mode=self.mode,
            local_path=local_path,
            status=StorageMountStatus.Failed,
            command=command,
            env_keys=sorted(env),
            output=output,
            reason=mounted.reason,
        )

    def unmount(self, local_path: str) -> StorageMountResult:
        result = retry_force_unmount(local_path, self.system, mode=self.mode)
        self._terminate_mount_cmd()
        with suppress(OSError):
            Path(local_path).rmdir()
        return result.model_copy(update={"mode": self.mode})

    def _terminate_mount_cmd(self) -> None:
        _terminate_managed_mount(self.mount_cmd)
        self.mount_cmd = None


def mountpoint_command(config: MountPointConfig, local_path: str) -> list[str]:
    command = [
        config.binary,
        config.bucket_name,
        local_path,
        "--foreground",
        "--auto-unmount",
        "--allow-other",
        f"--log-directory={config.log_directory}",
        "--upload-checksums=off",
    ]
    if config.prefix:
        command.append(f"--prefix={config.prefix}")
    if config.read_only:
        command.append("--read-only")
    else:
        command.extend(["--allow-delete", "--allow-overwrite"])
    if config.force_path_style:
        command.append("--force-path-style")
    if config.endpoint_url:
        command.append(f"--endpoint-url={config.endpoint_url}")
    if config.region:
        command.append(f"--region={config.region}")
    return command


def mountpoint_env(config: MountPointConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    if config.access_key:
        env["AWS_ACCESS_KEY_ID"] = config.access_key
    if config.secret_key:
        env["AWS_SECRET_ACCESS_KEY"] = config.secret_key
    return env


def retry_force_unmount(
    local_path: str,
    system: StorageMountSystem,
    *,
    mode: StorageMountMode,
    commands: Iterable[list[str]] = (),
    retries: int = DEFAULT_CLEANUP_RETRIES,
) -> StorageMountResult:
    if not system.mount_checker(local_path):
        return _status_result(
            mode,
            local_path,
            StorageMountStatus.AlreadyUnmounted,
        )
    attempts = [
        *commands,
        ["fusermount3", "-uz", local_path],
        ["fusermount", "-uz", local_path],
        ["umount", "-l", local_path],
    ]
    output: list[str] = []
    for _ in range(max(retries, 1)):
        for command in attempts:
            result = system.run_command(DEFAULT_UNMOUNT_TIMEOUT_SECONDS, command)
            output.append(result.stdout or result.stderr)
            if result.ok or not system.mount_checker(local_path):
                return _status_result(
                    mode,
                    local_path,
                    StorageMountStatus.Unmounted,
                    command=command,
                    reason="storage mount unmounted",
                )
        time.sleep(DEFAULT_MOUNT_POLL_SECONDS)
    return _status_result(
        mode,
        local_path,
        StorageMountStatus.Failed,
        reason="\n".join(part for part in output if part).strip()
        or "storage mount did not unmount",
    )


def is_mounted(mount_point: str) -> bool:
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return False
    return mount_info_contains(mountinfo.read_text(encoding="utf-8", errors="replace"), mount_point)


def mount_info_contains(text: str, mount_point: str) -> bool:
    target = str(Path(mount_point).resolve())
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        if _unescape_mountinfo_path(fields[4]) == target:
            return True
    return False


def unresolvable_endpoint_host(endpoint_url: str) -> str:
    """Return the endpoint host when this machine cannot resolve it.

    A mount tool given an unresolvable host neither connects nor exits: it retries
    until the mount times out, and the only text it produces is unrelated startup
    noise. Checking first turns a silent timeout into a named failure that says
    which host could not be reached.
    """
    if not endpoint_url:
        return ""
    host = urlsplit(endpoint_url).hostname or ""
    if not host:
        return ""
    try:
        socket.getaddrinfo(host, None)
    except OSError:
        return host
    return ""


def _wait_for_mount(
    local_path: str,
    mounted: MountChecker,
    command: StorageManagedCommand,
    *,
    mode: StorageMountMode,
    timeout_seconds: float,
) -> StorageMountResult:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if mounted(local_path):
            return _status_result(
                mode,
                local_path,
                StorageMountStatus.Mounted,
                reason="storage mount is ready",
            )
        result = command.poll()
        if result is not None:
            return _status_result(
                mode,
                local_path,
                StorageMountStatus.Failed,
                command=result.args,
                reason=f"mount command exited before mount completed: {result.exit_code}",
                output=result.output,
            )
        time.sleep(DEFAULT_MOUNT_POLL_SECONDS)
    return _status_result(
        mode,
        local_path,
        StorageMountStatus.Failed,
        output=command.output(),
        reason="timed out waiting for storage mount",
    )


def _status_result(
    mode: StorageMountMode,
    local_path: str,
    status: StorageMountStatus,
    command: list[str] | None = None,
    env: dict[str, str] | None = None,
    *,
    output: str = "",
    reason: str = "",
) -> StorageMountResult:
    return StorageMountResult(
        mode=mode,
        local_path=local_path,
        status=status,
        command=command or [],
        env_keys=sorted(env or {}),
        output=output,
        reason=reason or status.value,
    )


def _unescape_mountinfo_path(value: str) -> str:
    replacements = {
        "\\040": " ",
        "\\011": "\t",
        "\\012": "\n",
        "\\134": "\\",
    }
    result = value
    for old, new in replacements.items():
        result = result.replace(old, new)
    return os.path.abspath(result)
