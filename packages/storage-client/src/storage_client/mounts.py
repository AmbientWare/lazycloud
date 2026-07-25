from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from foundation.process import (
    ManagedCommandResult,
    ManagedCommandStillRunning,
    ProcessResult,
    run_command_with_timeout,
    start_managed_command,
)
from pydantic import Field
from shared.app_identity import NAME
from shared.contracts import ContractModel

DEFAULT_MOUNT_TIMEOUT_SECONDS = 30.0
DEFAULT_FORMAT_TIMEOUT_SECONDS = 60.0
DEFAULT_UNMOUNT_TIMEOUT_SECONDS = 10.0
DEFAULT_MOUNT_POLL_SECONDS = 0.1
DEFAULT_CLEANUP_RETRIES = 3


class StorageMountMode(StrEnum):
    Local = "local"
    JuiceFs = "juicefs"
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


class JuiceFsMountConfig(ContractModel):
    redis_uri: str
    bucket: str
    access_key: str = ""
    secret_key: str = ""
    cache_size: int = 0
    block_size: int = 4096
    prefetch: int = 1
    buffer_size: int = 300
    filesystem_name: str = NAME
    binary: str = "juicefs"


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


class StorageMountManager:
    mode: StorageMountMode

    def mount(self, local_path: str) -> StorageMountResult:
        raise NotImplementedError

    def unmount(self, local_path: str) -> StorageMountResult:
        raise NotImplementedError


@dataclass(slots=True)
class JuiceFsMountManager(StorageMountManager):
    config: JuiceFsMountConfig
    system: StorageMountSystem = field(default_factory=StorageMountSystem)
    mount_cmd: StorageManagedCommand | None = None
    mode: StorageMountMode = StorageMountMode.JuiceFs

    def format(self) -> StorageMountResult:
        command = [
            self.config.binary,
            "format",
            "--storage",
            "s3",
            "--bucket",
            self.config.bucket,
            "--block-size",
            str(self.config.block_size or 4096),
            self.config.redis_uri,
            self.config.filesystem_name,
            "--no-update",
        ]
        env = self._env()
        if self.config.access_key or self.config.secret_key:
            command.extend(
                [
                    "--access-key",
                    self.config.access_key,
                    "--secret-key",
                    self.config.secret_key,
                ]
            )
        result = self.system.run_command(DEFAULT_FORMAT_TIMEOUT_SECONDS, command)
        return _process_result(self.mode, "", command, env, result, "juicefs formatted")

    def mount(self, local_path: str) -> StorageMountResult:
        Path(local_path).mkdir(parents=True, exist_ok=True)
        if self.system.mount_checker(local_path):
            return _status_result(self.mode, local_path, StorageMountStatus.AlreadyMounted)
        command = [
            self.config.binary,
            "mount",
            self.config.redis_uri,
            local_path,
            "--bucket",
            self.config.bucket,
            "--cache-size",
            str(max(self.config.cache_size, 0)),
            "--prefetch",
            str(self.config.prefetch or 1),
            "--buffer-size",
            str(self.config.buffer_size or 300),
            "--no-usage-report",
        ]
        env = self._env()
        self.mount_cmd = self.system.start_command(command, env or None)
        mounted = _wait_for_mount(
            local_path,
            self.system.mount_checker,
            self.mount_cmd,
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
        result = retry_force_unmount(
            local_path,
            self.system,
            commands=([self.config.binary, "umount", local_path],),
        )
        self._terminate_mount_cmd()
        return result.model_copy(update={"mode": self.mode})

    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.config.access_key:
            env["AWS_ACCESS_KEY_ID"] = self.config.access_key
        if self.config.secret_key:
            env["AWS_SECRET_ACCESS_KEY"] = self.config.secret_key
            env["JUICEFS_SECRET_KEY"] = self.config.secret_key
        return env

    def _terminate_mount_cmd(self) -> None:
        if self.mount_cmd is None:
            return
        try:
            self.mount_cmd.terminate(timeout_seconds=DEFAULT_UNMOUNT_TIMEOUT_SECONDS)
        except ManagedCommandStillRunning:
            pass
        finally:
            self.mount_cmd = None


@dataclass(slots=True)
class MountPointMountManager(StorageMountManager):
    config: MountPointConfig
    system: StorageMountSystem = field(default_factory=StorageMountSystem)
    mount_cmd: StorageManagedCommand | None = None
    mode: StorageMountMode = StorageMountMode.MountPoint

    def mount(self, local_path: str) -> StorageMountResult:
        self.unmount(local_path)
        Path(local_path).mkdir(parents=True, exist_ok=True)
        command = mountpoint_command(self.config, local_path)
        env = mountpoint_env(self.config)
        self.mount_cmd = self.system.start_command(command, env or None)
        mounted = _wait_for_mount(
            local_path,
            self.system.mount_checker,
            self.mount_cmd,
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
        result = retry_force_unmount(local_path, self.system)
        self._terminate_mount_cmd()
        with suppress(OSError):
            Path(local_path).rmdir()
        return result.model_copy(update={"mode": self.mode})

    def _terminate_mount_cmd(self) -> None:
        if self.mount_cmd is None:
            return
        try:
            self.mount_cmd.terminate(timeout_seconds=DEFAULT_UNMOUNT_TIMEOUT_SECONDS)
        except ManagedCommandStillRunning:
            pass
        finally:
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
    commands: Iterable[list[str]] = (),
    retries: int = DEFAULT_CLEANUP_RETRIES,
) -> StorageMountResult:
    if not system.mount_checker(local_path):
        return _status_result(
            StorageMountMode.Local,
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
                    StorageMountMode.Local,
                    local_path,
                    StorageMountStatus.Unmounted,
                    command=command,
                    reason="storage mount unmounted",
                )
        time.sleep(DEFAULT_MOUNT_POLL_SECONDS)
    return _status_result(
        StorageMountMode.Local,
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


def _wait_for_mount(
    local_path: str,
    mounted: MountChecker,
    command: StorageManagedCommand,
    *,
    timeout_seconds: float,
) -> StorageMountResult:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if mounted(local_path):
            return _status_result(
                StorageMountMode.Local,
                local_path,
                StorageMountStatus.Mounted,
                reason="storage mount is ready",
            )
        result = command.poll()
        if result is not None:
            return _status_result(
                StorageMountMode.Local,
                local_path,
                StorageMountStatus.Failed,
                command=result.args,
                reason=f"mount command exited before mount completed: {result.exit_code}",
                output=result.output,
            )
        time.sleep(DEFAULT_MOUNT_POLL_SECONDS)
    return _status_result(
        StorageMountMode.Local,
        local_path,
        StorageMountStatus.Failed,
        output=command.output(),
        reason="timed out waiting for storage mount",
    )


def _process_result(
    mode: StorageMountMode,
    local_path: str,
    command: list[str],
    env: dict[str, str],
    result: ProcessResult,
    success_reason: str,
) -> StorageMountResult:
    return StorageMountResult(
        mode=mode,
        local_path=local_path,
        status=StorageMountStatus.Mounted if result.ok else StorageMountStatus.Failed,
        command=command,
        env_keys=sorted(env),
        exit_code=result.exit_code,
        output="\n".join(part for part in (result.stdout, result.stderr) if part).strip(),
        reason=success_reason if result.ok else "storage command failed",
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
