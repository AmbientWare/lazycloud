from __future__ import annotations

import builtins
import math
import shlex
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar
from uuid import UUID

from pydantic import BaseModel, JsonValue, ValidationError
from shared.api import (
    CommandItem,
    ContainerFile,
    ContainerLifecycle,
    ContainerState,
    CreateInstanceRequest,
    ErrorCode,
    FileMatch,
    FindInFilesRequest,
    Instance,
    NetworkPolicy,
    PodKind,
    PodSpec,
    Process,
    ProcessRequest,
    ReplaceInFilesRequest,
    SandboxStatus,
    StopReason,
    WorkloadSpec,
)
from shared.api import Sandbox as SandboxRow
from shared.api import SandboxStats as SandboxStatsResponse
from shared.app_identity import SANDBOX_COMPOSE_OVERRIDE_PATH
from shared.deployment_records import (
    DEFAULT_DISK,
    DEFAULT_WORKLOAD_PREEMPTIBLE,
    CpuRequest,
    DeploymentSpec,
    MemoryRequest,
    Resources,
    VolumeMount,
    request_and_limit,
)
from shared.deployments import DeploymentKind
from shared.gpu import GpuInput, gpu_preference
from shared.placement import ProductRegion
from typing_extensions import Never, Self

from lazycloud.abstractions.image import Image
from lazycloud.abstractions.metadata import MachineInput, build_resource_metadata
from lazycloud.abstractions.volume import VolumeExport, volume_mounts
from lazycloud.aio import to_thread
from lazycloud.clients.api import ApiError
from lazycloud.clients.workloads import CONNECT_WAIT_SECONDS, PROCESS_WAIT_SECONDS, WorkloadsClient
from lazycloud.control import ControlClientConfigMixin, workloads_client
from lazycloud.exceptions import SdkError, UnsupportedFeatureError
from lazycloud.json_contracts import validate_json_object
from lazycloud.session.task import retry_transient
from lazycloud.terminal import Terminal

if TYPE_CHECKING:
    from lazycloud.abstractions.image import ImageBuildResult

T = TypeVar("T")

SANDBOX_CONTROL_TIMEOUT_SECONDS = 30.0
SANDBOX_READY_TIMEOUT_SECONDS = 120.0
SANDBOX_WAIT_POLL_INTERVAL_SECONDS = 0.5
_DOCKER_SANDBOX_SECURITY_OPTIONS = ("systempaths=unconfined",)
# Stop reasons that mean a sandbox failed rather than was stopped.
_FAILED_STOP_REASONS = frozenset(
    {
        StopReason.load_error,
        StopReason.start_failed,
        StopReason.crashed,
        StopReason.out_of_memory,
        StopReason.host_lost,
    }
)


class SandboxOptions(TypedDict, total=False):
    cpu: CpuRequest | str
    memory: MemoryRequest
    disk: str | None
    gpu: GpuInput
    gpu_count: int
    image: Image | None
    keep_warm_seconds: int
    authorized: bool
    name: str | None
    volumes: Iterable[VolumeMount | VolumeExport] | None
    secrets: Iterable[str] | None
    env: Mapping[str, str] | None
    sync_local_dir: bool
    block_network: bool
    allow_list: Iterable[str] | None
    docker_enabled: bool
    preemptible: bool
    ports: Iterable[int] | None
    region: str | None
    availability_zone: str
    machine: MachineInput
    metadata: Mapping[str, Any] | None
    command: Iterable[str] | None


class SandboxConnectionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        container_id: str = "",
        state: str = "",
        cleanup_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.container_id = container_id
        self.state = state
        self.cleanup_error = cleanup_error


class SandboxProcessError(RuntimeError):
    pass


class SandboxProcessTimeoutError(SandboxProcessError):
    pass


class SandboxFileSystemError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        path: str = "",
        container_id: str = "",
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.path = path
        self.container_id = container_id


@dataclass(frozen=True, slots=True)
class SandboxFilePosition:
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class SandboxFileSearchRange:
    start: SandboxFilePosition
    end: SandboxFilePosition


@dataclass(frozen=True, slots=True)
class SandboxFileSearchMatch:
    range: SandboxFileSearchRange
    content: str


@dataclass(frozen=True, slots=True)
class SandboxFileSearchResult:
    path: str
    matches: list[SandboxFileSearchMatch]


@dataclass(frozen=True, slots=True)
class SandboxFileInfo:
    name: str
    is_dir: bool
    size: int
    mode: int = 0
    mod_time: datetime | None = None
    permissions: int = 0
    owner: str = ""
    group: str = ""


@dataclass(frozen=True, slots=True)
class SandboxProcessInfo:
    pid: int
    command: str


@dataclass(frozen=True, slots=True)
class SandboxNetworkPolicy:
    block_network: bool
    allow_list: list[str]


@dataclass(frozen=True, slots=True)
class SandboxProcessResponse:
    """Captured output prefixes; truncation flags report output omitted by storage limits."""

    pid: int
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def result(self) -> str:
        return self.stdout + self.stderr


class SandboxProcessStream:
    def __init__(self, process: SandboxProcess, fetch_output: Callable[[], str]) -> None:
        self._process = process
        self._fetch_output = fetch_output
        self._last_output = ""
        self._buffer = ""
        self._closed = False

    def read(self) -> str:
        data = self._buffer + self._fetch_next_chunk()
        self._buffer = ""
        return data

    def lines(self) -> list[str]:
        return self.read().splitlines()

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> str:
        while True:
            if "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                return f"{line}\n"

            if self._closed:
                if self._buffer:
                    line, self._buffer = self._buffer, ""
                    return line
                raise StopIteration

            chunk = self._fetch_next_chunk()
            if chunk:
                self._buffer += chunk
                continue

            exit_code, _ = self._process.status()
            if exit_code >= 0:
                final_chunk = self._fetch_next_chunk()
                if final_chunk:
                    self._buffer += final_chunk
                    continue
                self._closed = True
                continue
            time.sleep(SANDBOX_WAIT_POLL_INTERVAL_SECONDS)

    def _fetch_next_chunk(self) -> str:
        output = self._fetch_output()
        chunk = output[len(self._last_output) :] if output.startswith(self._last_output) else output
        self._last_output = output
        return chunk


class _SandboxCombinedStream:
    def __init__(self, process: SandboxProcess) -> None:
        self.process = process

    def read(self) -> str:
        return self.process.stdout.read() + self.process.stderr.read()

    def __iter__(self) -> Iterator[str]:
        return iter(self.read().splitlines())


class SandboxTimeline(BaseModel):
    """When a sandbox container was created, placed, ready and stopped."""

    container_id: str | None = None
    status: SandboxStatus
    created_at: datetime
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    scheduling_ms: int | None = None
    startup_ms: int | None = None
    runtime_ms: int | None = None


@dataclass(slots=True)
class SandboxProcess:
    container_id: str
    pid: int
    client: WorkloadsClient
    process_id: str
    cwd: str = "/workspace"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    exit_code: int = -1
    _status: str = ""
    _stdout_stream: SandboxProcessStream | None = field(default=None, init=False, repr=False)
    _stderr_stream: SandboxProcessStream | None = field(default=None, init=False, repr=False)
    _logs_stream: _SandboxCombinedStream | None = field(default=None, init=False, repr=False)
    _result: Process | None = field(default=None, init=False, repr=False)

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            response = self._poll_result(deadline, timeout)
            if not response.running:
                return _exit_code(response)

    def _poll_result(self, deadline: float | None, timeout: float | None) -> Process:
        wait_seconds = (
            PROCESS_WAIT_SECONDS
            if deadline is None
            else max(0.0, min(PROCESS_WAIT_SECONDS, deadline - time.monotonic()))
        )
        response = self._read_result(wait_seconds)
        if response.running and deadline is not None and time.monotonic() >= deadline:
            msg = f"process {self.pid} did not exit within {timeout} seconds"
            raise SandboxProcessTimeoutError(msg)
        return response

    def result(self, timeout: float | None = None) -> SandboxProcessResponse:
        self.wait(timeout)
        response = self._read_result(0)
        return SandboxProcessResponse(
            pid=self.pid,
            exit_code=_exit_code(response),
            stdout=response.stdout,
            stderr=response.stderr,
            stdout_truncated=response.stdout_truncated,
            stderr_truncated=response.stderr_truncated,
        )

    def _read_result(self, wait_seconds: float) -> Process:
        if self._result is not None:
            return self._result
        response = _process_call(
            lambda: self.client.get_process(
                UUID(self.container_id), self.process_id, wait_seconds=wait_seconds
            ),
            f"process {self.pid}",
        )
        if not response.running:
            self._result = response
            self.exit_code = _exit_code(response)
            self._status = "exited"
        return response

    def kill(self) -> None:
        _process_call(
            lambda: self.client.kill_process(UUID(self.container_id), self.process_id),
            f"process {self.pid}",
        )

    def status(self) -> tuple[int, str]:
        response = self._read_result(0)
        return _exit_code(response), "running" if response.running else "exited"

    @property
    def stdout(self) -> SandboxProcessStream:
        if self._stdout_stream is None:
            self._stdout_stream = SandboxProcessStream(self, self._stdout)
        return self._stdout_stream

    @property
    def stderr(self) -> SandboxProcessStream:
        if self._stderr_stream is None:
            self._stderr_stream = SandboxProcessStream(self, self._stderr)
        return self._stderr_stream

    @property
    def logs(self) -> _SandboxCombinedStream:
        if self._logs_stream is None:
            self._logs_stream = _SandboxCombinedStream(self)
        return self._logs_stream

    def _stdout(self) -> str:
        return self._read_result(0).stdout

    def _stderr(self) -> str:
        return self._read_result(0).stderr

    @property
    def aio(self) -> AsyncSandboxProcess:
        return AsyncSandboxProcess(self)


@dataclass(slots=True)
class AsyncSandboxProcess:
    process: SandboxProcess

    async def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            response = await to_thread(self.process._poll_result, deadline, timeout)
            if not response.running:
                return _exit_code(response)

    async def result(self, timeout: float | None = None) -> SandboxProcessResponse:
        await self.wait(timeout)
        return self.process.result(0)

    async def kill(self) -> None:
        await to_thread(self.process.kill)

    async def status(self) -> tuple[int, str]:
        return await to_thread(self.process.status)

    async def stdout(self) -> str:
        return await to_thread(self.process.stdout.read)

    async def stderr(self) -> str:
        return await to_thread(self.process.stderr.read)

    async def logs(self) -> str:
        return await to_thread(self.process.logs.read)


@dataclass(slots=True)
class SandboxProcessManager:
    container_id: str
    client: WorkloadsClient

    def run(
        self,
        command: str | Iterable[str],
        *,
        timeout_seconds: float | None = None,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> SandboxProcessResponse:
        process = self._exec_command(command, cwd=cwd, env=env)
        try:
            return process.result(timeout_seconds)
        except SandboxProcessTimeoutError:
            process.kill()
            process.wait(timeout=SANDBOX_CONTROL_TIMEOUT_SECONDS)
            raise

    def run_code(
        self,
        code: str,
        *,
        blocking: bool = True,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> SandboxProcessResponse | SandboxProcess:
        process = self.exec("python3", "-c", code, cwd=cwd, env=env)
        if not blocking:
            return process
        return process.result()

    def exec(
        self,
        *args: str,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> SandboxProcess:
        if not args:
            msg = "at least one command argument is required"
            raise SandboxProcessError(msg)
        return self._exec_command(args, cwd=cwd, env=env)

    def list_processes(self) -> dict[int, SandboxProcess]:
        response = _process_call(
            lambda: self.client.list_processes(UUID(self.container_id)), "processes"
        )
        return {
            item.pid: SandboxProcess(
                container_id=self.container_id,
                pid=item.pid,
                process_id=item.process_id,
                client=self.client,
                args=item.command.split(),
                cwd=item.cwd or "/workspace",
            )
            for item in response.processes
        }

    def _exec_command(
        self,
        command: str | Iterable[str],
        *,
        cwd: str,
        env: dict[str, str] | None,
    ) -> SandboxProcess:
        args = _command_args_list(command)
        try:
            fields: dict[str, Any] = {"args": args, "cwd": cwd}
            if env:
                fields["env"] = dict(env)
            request = ProcessRequest.model_validate(fields)
        except ValidationError as exc:
            raise SandboxProcessError(f"invalid sandbox command: {exc}") from exc
        response = _process_call(
            lambda: self.client.start_process(UUID(self.container_id), request), "process"
        )
        if response.pid <= 0:
            msg = "sandbox process did not return a valid pid"
            raise SandboxProcessError(msg)
        return SandboxProcess(
            container_id=self.container_id,
            pid=response.pid,
            process_id=response.process_id,
            client=self.client,
            cwd=cwd,
            args=args,
            env=dict(env or {}),
        )

    @property
    def aio(self) -> AsyncSandboxProcessManager:
        return AsyncSandboxProcessManager(self)


@dataclass(slots=True)
class AsyncSandboxProcessManager:
    process: SandboxProcessManager

    async def run(
        self,
        command: str | Iterable[str],
        *,
        timeout_seconds: float | None = None,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> SandboxProcessResponse:
        process = await to_thread(self.process._exec_command, command, cwd=cwd, env=env)
        try:
            return await process.aio.result(timeout_seconds)
        except SandboxProcessTimeoutError:
            await process.aio.kill()
            await process.aio.wait(SANDBOX_CONTROL_TIMEOUT_SECONDS)
            raise

    async def run_code(
        self,
        code: str,
        *,
        blocking: bool = True,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> SandboxProcessResponse | SandboxProcess:
        process = await self.exec("python3", "-c", code, cwd=cwd, env=env)
        if not blocking:
            return process
        return await process.aio.result()

    async def exec(
        self,
        *args: str,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> SandboxProcess:
        return await to_thread(self.process.exec, *args, cwd=cwd, env=env)

    async def list_processes(self) -> dict[int, SandboxProcess]:
        return await to_thread(self.process.list_processes)


@dataclass(slots=True)
class SandboxFileSystem:
    container_id: str
    client: WorkloadsClient

    def stat_file(self, sandbox_path: str | Path) -> SandboxFileInfo:
        path_text = _path_text(sandbox_path)
        response = self._call("stat_file", path_text, self.client.stat_file)
        return _sandbox_file_info(response, requested_path=path_text)

    def _write_text(self, sandbox_path: str | Path, content: str) -> None:
        self._upload_bytes(sandbox_path, content.encode("utf-8"))

    def _read_text(self, sandbox_path: str | Path) -> str:
        return self._download_bytes(sandbox_path).decode("utf-8")

    def _upload_bytes(self, path: str | Path, content: bytes, *, mode: int = 0o644) -> None:
        self._call(
            "upload_file",
            _path_text(path),
            lambda container, path_text: self.client.upload_file(
                container, path_text, content, mode=mode
            ),
        )

    def _download_bytes(self, path: str | Path) -> bytes:
        return self._call("download_file", _path_text(path), self.client.download_file).data

    def upload_file(self, local_path: str | Path, sandbox_path: str | Path) -> None:
        self._upload_bytes(sandbox_path, Path(local_path).read_bytes())

    def download_file(self, sandbox_path: str | Path, local_path: str | Path) -> None:
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._download_bytes(sandbox_path))

    def create_directory(self, sandbox_path: str | Path, *, mode: int = 0o755) -> None:
        self._create_directory(sandbox_path, mode=mode)

    def _create_directory(self, path: str | Path, *, mode: int = 0o755) -> str:
        path_text = _path_text(path)
        self._call(
            "create_directory",
            path_text,
            lambda container, selected: self.client.create_directory(
                container, selected, mode=mode
            ),
        )
        return path_text

    def delete_directory(self, sandbox_path: str | Path) -> None:
        self._call("delete_directory", _path_text(sandbox_path), self.client.delete_directory)

    def delete_file(self, sandbox_path: str | Path) -> None:
        self._call("delete_file", _path_text(sandbox_path), self.client.delete_file)

    def list_files(self, sandbox_path: str | Path) -> list[SandboxFileInfo]:
        path_text = _path_text(sandbox_path)
        response = self._call("list_files", path_text, self.client.list_files)
        if response.truncated:
            raise SandboxFileSystemError(
                f"{path_text} holds more entries than one listing returns",
                operation="list_files",
                path=path_text,
                container_id=self.container_id,
            )
        return [
            _sandbox_file_info(
                file_info,
                requested_path=_join_remote_path(path_text, file_info.name),
            )
            for file_info in response.files
        ]

    def find_in_files(
        self,
        sandbox_path: str | Path,
        pattern: str,
    ) -> list[SandboxFileSearchResult]:
        path_text = _path_text(sandbox_path)
        response = self._call(
            "find_in_files",
            path_text,
            lambda container, selected: self.client.find_in_files(
                container, FindInFilesRequest(path=selected, pattern=pattern)
            ),
        )
        return _sandbox_search_results(response.matches, pattern)

    def replace_in_files(
        self,
        sandbox_path: str | Path,
        old_string: str,
        new_string: str,
    ) -> None:
        self._call(
            "replace_in_files",
            _path_text(sandbox_path),
            lambda container, selected: self.client.replace_in_files(
                container,
                ReplaceInFilesRequest(path=selected, pattern=old_string, replacement=new_string),
            ),
        )

    def _call(self, operation: str, path: str, call: Callable[[UUID, str], T]) -> T:
        """Run one file operation; a request the API refuses as invalid names the path."""
        try:
            return call(UUID(self.container_id), path)
        except ValidationError as exc:
            raise SandboxFileSystemError(
                f"{operation} {path}: {exc}",
                operation=operation,
                path=path,
                container_id=self.container_id,
            ) from exc

    @property
    def aio(self) -> AsyncSandboxFileSystem:
        return AsyncSandboxFileSystem(self)


@dataclass(slots=True)
class AsyncSandboxFileSystem:
    filesystem: SandboxFileSystem

    async def stat_file(self, sandbox_path: str | Path) -> SandboxFileInfo:
        return await to_thread(self.filesystem.stat_file, sandbox_path)

    async def upload_file(self, local_path: str | Path, sandbox_path: str | Path) -> None:
        await to_thread(self.filesystem.upload_file, local_path, sandbox_path)

    async def download_file(self, sandbox_path: str | Path, local_path: str | Path) -> None:
        await to_thread(self.filesystem.download_file, sandbox_path, local_path)

    async def list_files(self, sandbox_path: str | Path) -> list[SandboxFileInfo]:
        return await to_thread(self.filesystem.list_files, sandbox_path)

    async def find_in_files(
        self,
        sandbox_path: str | Path,
        pattern: str,
    ) -> list[SandboxFileSearchResult]:
        return await to_thread(self.filesystem.find_in_files, sandbox_path, pattern)

    async def replace_in_files(
        self,
        sandbox_path: str | Path,
        old_string: str,
        new_string: str,
    ) -> None:
        await to_thread(
            self.filesystem.replace_in_files,
            sandbox_path,
            old_string,
            new_string,
        )

    async def create_directory(self, sandbox_path: str | Path, *, mode: int = 0o755) -> None:
        await to_thread(self.filesystem.create_directory, sandbox_path, mode=mode)

    async def delete_directory(self, sandbox_path: str | Path) -> None:
        await to_thread(self.filesystem.delete_directory, sandbox_path)

    async def delete_file(self, sandbox_path: str | Path) -> None:
        await to_thread(self.filesystem.delete_file, sandbox_path)


@dataclass(slots=True)
class DockerResult:
    command: list[str]
    response: SandboxProcessResponse | None = None
    process: SandboxProcess | None = None

    @property
    def exit_code(self) -> int:
        if self.response is not None:
            return self.response.exit_code
        return self.process.exit_code if self.process is not None else -1

    @property
    def stdout(self) -> str:
        if self.response is not None:
            return self.response.stdout
        return self.process.stdout.read() if self.process is not None else ""

    @property
    def stderr(self) -> str:
        if self.response is not None:
            return self.response.stderr
        return self.process.stderr.read() if self.process is not None else ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def wait(self, timeout: float | None = None) -> bool:
        if self.process is None:
            return self.success
        return self.process.wait(timeout) == 0

    def logs(self) -> Iterable[str]:
        if self.process is not None:
            return self.process.logs
        return iter(self.output.splitlines(keepends=True))


@dataclass(slots=True)
class DockerComposeStack:
    docker: SandboxDockerManager
    file: str
    cwd: str
    override_path: str
    services: list[str]
    result: DockerResult

    def logs(
        self,
        service: str | None = None,
        *,
        follow: bool = False,
        tail: int | None = None,
    ) -> str:
        return self.docker.compose_logs(
            file=self.file,
            override_file=self.override_path,
            follow=follow,
            tail=tail,
            service=service,
            cwd=self.cwd,
        ).stdout

    def stop(self) -> DockerResult:
        return self.docker.compose_down(
            file=self.file,
            override_file=self.override_path,
            cwd=self.cwd,
        )

    def ps(self) -> str:
        return self.docker.compose_ps(
            file=self.file,
            override_file=self.override_path,
            cwd=self.cwd,
        ).stdout


@dataclass(slots=True)
class SandboxDockerManager:
    process: SandboxProcessManager
    filesystem: SandboxFileSystem | None = None
    daemon_timeout_seconds: float = 30.0
    _daemon_ready: bool = field(default=False, init=False, repr=False)

    def _wait_for_daemon(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.5,
    ) -> bool:
        deadline = time.monotonic() + (timeout_seconds or self.daemon_timeout_seconds)
        last_error = ""
        while True:
            try:
                response = self.process.run(
                    ["docker", "info"],
                    timeout_seconds=min(poll_interval_seconds, 5.0),
                )
                if response.exit_code == 0:
                    self._daemon_ready = True
                    return True
                last_error = response.stderr or response.stdout
            except SandboxProcessError as exc:
                last_error = str(exc)
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval_seconds)
        if last_error:
            return False
        return False

    def _ensure_daemon_ready(self, *, timeout_seconds: float | None = None) -> None:
        if self._daemon_ready:
            return
        if not self._wait_for_daemon(timeout_seconds=timeout_seconds):
            msg = "docker daemon is not ready in sandbox"
            raise SandboxProcessError(msg)

    def run(
        self,
        image: str,
        command: str | Sequence[str] | None = None,
        *,
        name: str | None = None,
        detach: bool = False,
        remove: bool = False,
        ports: Mapping[int | str, int | str] | None = None,
        env: Mapping[str, str] | None = None,
        volumes: Mapping[str, str] | Sequence[str] | None = None,
        cwd: str = "/workspace",
    ) -> DockerResult:
        if ports:
            raise SandboxProcessError(
                "Nested Docker shares the sandbox network and cannot publish port mappings. "
                "Bind the service to its sandbox port and use instance.expose_port(port)."
            )
        args = ["docker", "run"]
        if detach:
            args.append("--detach")
        if remove:
            args.append("--rm")
        args.extend(["--network", "host", "--pid", "host"])
        for option in _DOCKER_SANDBOX_SECURITY_OPTIONS:
            args.extend(["--security-opt", option])
        if name:
            args.extend(["--name", name])
        args.extend(_docker_env_args(env or {}))
        args.extend(_docker_volume_args(volumes or {}))
        args.append(image)
        if command:
            args.extend(_command_args(command))
        return self._execute(args, detach=detach, cwd=cwd)

    def build(
        self,
        tag: str,
        context: str = ".",
        *,
        dockerfile: str | None = None,
        build_args: Mapping[str, str] | None = None,
        network: str = "host",
        cwd: str = "/workspace",
    ) -> DockerResult:
        args = ["docker", "build"]
        if network:
            args.extend(["--network", network])
        args.extend(["-t", tag])
        if dockerfile:
            args.extend(["-f", dockerfile])
        for key, value in (build_args or {}).items():
            args.extend(["--build-arg", f"{key}={value}"])
        args.append(context)
        return self._execute(args, cwd=cwd)

    def pull(self, image: str) -> DockerResult:
        return self._execute(["docker", "pull", image])

    def push(self, image: str) -> DockerResult:
        return self._execute(["docker", "push", image])

    def tag(self, source: str, target: str) -> DockerResult:
        return self._execute(["docker", "tag", source, target])

    def ps(self, *, all: bool = False) -> DockerResult:
        args = ["docker", "ps"]
        if all:
            args.append("--all")
        return self._execute(args)

    def images(self) -> DockerResult:
        return self._execute(["docker", "images"])

    def logs(self, container: str) -> DockerResult:
        return self._execute(["docker", "logs", container])

    def stop(self, *containers: str) -> DockerResult:
        return self._execute(["docker", "stop", *containers])

    def rm(self, *containers: str, force: bool = False) -> DockerResult:
        args = ["docker", "rm"]
        if force:
            args.append("--force")
        args.extend(containers)
        return self._execute(args)

    def rmi(self, *images: str, force: bool = False) -> DockerResult:
        args = ["docker", "rmi"]
        if force:
            args.append("--force")
        args.extend(images)
        return self._execute(args)

    def exec(self, container: str, command: str | Sequence[str]) -> DockerResult:
        return self._execute(["docker", "exec", container, *_command_args(command)])

    def login(
        self,
        registry: str = "",
        *,
        username: str,
        password: str | None = None,
        password_stdin_env: str = "DOCKER_PASSWORD",
    ) -> DockerResult:
        registry_arg = f" {shlex.quote(registry)}" if registry else ""
        result = self._execute(
            [
                "sh",
                "-lc",
                f'printf %s "${{{password_stdin_env}}}" | docker login{registry_arg} '
                f"--username {shlex.quote(username)} --password-stdin",
            ],
            env={password_stdin_env: password} if password is not None else None,
        )
        return result

    def _compose_override(
        self,
        *,
        file: str = "docker-compose.yml",
        output_file: str = SANDBOX_COMPOSE_OVERRIDE_PATH,
        cwd: str = "/workspace",
    ) -> str:
        import yaml

        if self.filesystem is None:
            msg = "compose override generation requires sandbox filesystem access"
            raise SandboxProcessError(msg)
        compose_path = _remote_path(file, cwd=cwd)
        try:
            raw_compose_config = yaml.safe_load(self.filesystem._read_text(compose_path))
            compose_config = validate_json_object(raw_compose_config or {})
        except (SandboxFileSystemError, ValueError, yaml.YAMLError) as exc:
            msg = f"failed to read compose file {compose_path}: {exc}"
            raise SandboxProcessError(msg) from exc
        services = compose_config.get("services", {})
        if not isinstance(services, dict):
            msg = f"compose file {compose_path} must contain a services mapping"
            raise SandboxProcessError(msg)
        service_names = [str(name) for name in services]
        override_services: dict[str, dict[str, JsonValue]] = {}
        for name, config in services.items():
            service_name = str(name)
            if isinstance(config, dict) and config.get("ports"):
                raise SandboxProcessError(
                    f"Compose service {service_name} declares port mappings, but nested Docker "
                    "shares the sandbox network. Remove ports, bind the service to its sandbox "
                    "port and use instance.expose_port(port)."
                )
            service_override: dict[str, JsonValue] = {
                "network_mode": "host",
                "pid": "host",
                "security_opt": list(_DOCKER_SANDBOX_SECURITY_OPTIONS),
                "extra_hosts": [
                    f"{peer}:127.0.0.1" for peer in service_names if peer != service_name
                ],
            }
            if isinstance(config, dict) and "build" in config:
                service_override["build"] = _compose_build_override(config["build"])
            override_services[service_name] = service_override
        override = {"services": override_services}
        output_path = _remote_path(output_file, cwd=cwd)
        self.filesystem._write_text(output_path, yaml.safe_dump(override, sort_keys=False))
        return output_path

    def compose_up(
        self,
        *,
        file: str = "docker-compose.yml",
        override_file: str | None = None,
        gvisor_safe: bool = True,
        detach: bool = True,
        build: bool = False,
        cwd: str = "/workspace",
    ) -> DockerComposeStack:
        args, selected_override = self._compose_args(
            file,
            override_file,
            gvisor_safe=gvisor_safe,
            cwd=cwd,
        )
        args.append("up")
        if detach:
            args.append("--detach")
        if build:
            args.append("--build")
        result = self._execute(args, cwd=cwd)
        return DockerComposeStack(
            docker=self,
            file=file,
            cwd=cwd,
            override_path=selected_override,
            services=self._compose_service_names(file=file, cwd=cwd),
            result=result,
        )

    def compose_down(
        self,
        *,
        file: str = "docker-compose.yml",
        override_file: str | None = None,
        gvisor_safe: bool = True,
        volumes: bool = False,
        remove_orphans: bool = False,
        cwd: str = "/workspace",
    ) -> DockerResult:
        args, _ = self._compose_args(file, override_file, gvisor_safe=gvisor_safe, cwd=cwd)
        args.append("down")
        if volumes:
            args.append("--volumes")
        if remove_orphans:
            args.append("--remove-orphans")
        return self._execute(args, cwd=cwd)

    def compose_logs(
        self,
        *,
        file: str = "docker-compose.yml",
        override_file: str | None = None,
        gvisor_safe: bool = True,
        follow: bool = False,
        tail: int | None = None,
        service: str | None = None,
        cwd: str = "/workspace",
    ) -> DockerResult:
        args, _ = self._compose_args(file, override_file, gvisor_safe=gvisor_safe, cwd=cwd)
        args.append("logs")
        if follow:
            args.append("--follow")
        if tail is not None:
            args.extend(["--tail", str(tail)])
        if service:
            args.append(service)
        return self._execute(args, detach=follow, cwd=cwd)

    def compose_ps(
        self,
        *,
        file: str = "docker-compose.yml",
        override_file: str | None = None,
        gvisor_safe: bool = True,
        cwd: str = "/workspace",
    ) -> DockerResult:
        args, _ = self._compose_args(file, override_file, gvisor_safe=gvisor_safe, cwd=cwd)
        args.append("ps")
        return self._execute(args, cwd=cwd)

    def compose_build(
        self,
        *,
        file: str = "docker-compose.yml",
        override_file: str | None = None,
        gvisor_safe: bool = True,
        no_cache: bool = False,
        pull: bool = False,
        cwd: str = "/workspace",
    ) -> DockerResult:
        args, _ = self._compose_args(file, override_file, gvisor_safe=gvisor_safe, cwd=cwd)
        args.append("build")
        if no_cache:
            args.append("--no-cache")
        if pull:
            args.append("--pull")
        return self._execute(args, cwd=cwd)

    def volume_create(self, name: str) -> DockerResult:
        return self._execute(["docker", "volume", "create", name])

    def volume_ls(self) -> DockerResult:
        return self._execute(["docker", "volume", "ls"])

    def volume_rm(self, *names: str) -> DockerResult:
        return self._execute(["docker", "volume", "rm", *names])

    @property
    def aio(self) -> AsyncSandboxDockerManager:
        return AsyncSandboxDockerManager(self)

    def _execute(
        self,
        args: list[str],
        *,
        detach: bool = False,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> DockerResult:
        self._ensure_daemon_ready()
        if detach:
            process = self.process.exec(*args, cwd=cwd, env=env)
            return DockerResult(command=args, process=process)
        return DockerResult(command=args, response=self.process.run(args, cwd=cwd, env=env))

    def _compose_args(
        self,
        file: str,
        override_file: str | None,
        *,
        gvisor_safe: bool,
        cwd: str,
    ) -> tuple[list[str], str]:
        selected_override = override_file
        if selected_override is None and gvisor_safe:
            selected_override = self._compose_override(file=file, cwd=cwd)
        args = ["docker", "compose", "-f", file]
        if selected_override:
            args.extend(["-f", selected_override])
        return args, selected_override or ""

    def _compose_service_names(self, *, file: str, cwd: str) -> list[str]:
        import yaml

        if self.filesystem is None:
            return []
        try:
            raw_compose_config = yaml.safe_load(
                self.filesystem._read_text(_remote_path(file, cwd=cwd))
            )
            compose_config = validate_json_object(raw_compose_config or {})
        except (SandboxFileSystemError, ValueError, yaml.YAMLError):
            return []
        services = compose_config.get("services", {})
        if not isinstance(services, dict):
            return []
        return [str(name) for name in services]


@dataclass(slots=True)
class AsyncSandboxDockerManager:
    docker: SandboxDockerManager

    async def run(self, *args: object, **kwargs: object) -> DockerResult:
        return await to_thread(self.docker.run, *args, **kwargs)

    async def build(self, *args: object, **kwargs: object) -> DockerResult:
        return await to_thread(self.docker.build, *args, **kwargs)

    async def pull(self, image: str) -> DockerResult:
        return await to_thread(self.docker.pull, image)

    async def push(self, image: str) -> DockerResult:
        return await to_thread(self.docker.push, image)

    async def tag(self, source: str, target: str) -> DockerResult:
        return await to_thread(self.docker.tag, source, target)

    async def ps(self, *, all: bool = False) -> DockerResult:
        return await to_thread(self.docker.ps, all=all)

    async def images(self) -> DockerResult:
        return await to_thread(self.docker.images)

    async def logs(self, container: str) -> DockerResult:
        return await to_thread(self.docker.logs, container)

    async def stop(self, *containers: str) -> DockerResult:
        return await to_thread(self.docker.stop, *containers)

    async def rm(self, *containers: str, force: bool = False) -> DockerResult:
        return await to_thread(self.docker.rm, *containers, force=force)

    async def rmi(self, *images: str, force: bool = False) -> DockerResult:
        return await to_thread(self.docker.rmi, *images, force=force)

    async def exec(self, container: str, command: str | Sequence[str]) -> DockerResult:
        return await to_thread(self.docker.exec, container, command)

    async def login(
        self,
        registry: str = "",
        *,
        username: str,
        password: str | None = None,
        password_stdin_env: str = "DOCKER_PASSWORD",
    ) -> DockerResult:
        return await to_thread(
            self.docker.login,
            registry,
            username=username,
            password=password,
            password_stdin_env=password_stdin_env,
        )

    async def compose_up(self, *args: object, **kwargs: object) -> DockerComposeStack:
        return await to_thread(self.docker.compose_up, *args, **kwargs)

    async def compose_down(self, *args: object, **kwargs: object) -> DockerResult:
        return await to_thread(self.docker.compose_down, *args, **kwargs)

    async def compose_logs(self, *args: object, **kwargs: object) -> DockerResult:
        return await to_thread(self.docker.compose_logs, *args, **kwargs)

    async def compose_ps(self, *args: object, **kwargs: object) -> DockerResult:
        return await to_thread(self.docker.compose_ps, *args, **kwargs)

    async def compose_build(self, *args: object, **kwargs: object) -> DockerResult:
        return await to_thread(self.docker.compose_build, *args, **kwargs)

    async def volume_create(self, name: str) -> DockerResult:
        return await to_thread(self.docker.volume_create, name)

    async def volume_ls(self) -> DockerResult:
        return await to_thread(self.docker.volume_ls)

    async def volume_rm(self, *names: str) -> DockerResult:
        return await to_thread(self.docker.volume_rm, *names)


@dataclass(slots=True)
class SandboxInstance:
    container_id: str
    stub_id: str
    client: WorkloadsClient
    terminated: bool = False
    _filesystem: SandboxFileSystem | None = field(default=None, init=False, repr=False)
    _process: SandboxProcessManager | None = field(default=None, init=False, repr=False)
    _docker: SandboxDockerManager | None = field(default=None, init=False, repr=False)

    @property
    def id(self) -> str:
        return self.container_id

    @property
    def fs(self) -> SandboxFileSystem:
        if self._filesystem is None:
            self._filesystem = SandboxFileSystem(self.container_id, self.client)
        return self._filesystem

    @property
    def process(self) -> SandboxProcessManager:
        if self._process is None:
            self._process = SandboxProcessManager(self.container_id, self.client)
        return self._process

    @property
    def docker(self) -> SandboxDockerManager:
        if self._docker is None:
            self._docker = SandboxDockerManager(self.process, filesystem=self.fs)
        return self._docker

    @property
    def aio(self) -> AsyncSandboxInstance:
        return AsyncSandboxInstance(self)

    def sandbox_id(self) -> str:
        return self.container_id

    def run(
        self,
        command: str | Iterable[str],
        *,
        timeout_seconds: float | None = None,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> SandboxProcessResponse:
        return self.process.run(command, timeout_seconds=timeout_seconds, cwd=cwd, env=env)

    def expose_port(self, port: int) -> str:
        return self._call(lambda container: self.client.expose_port(container, port)).url

    def update_network_permissions(
        self,
        *,
        block_network: bool = False,
        allow_list: list[str] | None = None,
    ) -> SandboxNetworkPolicy:
        if block_network and allow_list:
            msg = "block_network cannot be combined with allow_list"
            raise ValueError(msg)
        try:
            policy = NetworkPolicy.model_validate(
                {"block_network": block_network, "allow_list": list(allow_list or [])}
            )
        except ValidationError as exc:
            raise ValueError(f"invalid network permissions: {exc}") from exc
        response = self._call(lambda container: self.client.set_network(container, policy))
        return _network_policy(response)

    def network_permissions(self) -> SandboxNetworkPolicy:
        return _network_policy(self._call(self.client.network))

    def list_urls(self) -> dict[int, str]:
        response = self._call(self.client.list_ports)
        return {item.port: item.url for item in response.ports}

    def update_ttl(self, ttl: int) -> None:
        if self.terminated:
            raise SandboxConnectionError("sandbox is terminated")
        self._call(lambda container: self.client.set_ttl(container, ttl))

    def snapshot_memory(self) -> str:
        snapshot = self._call(self.client.snapshot)
        if snapshot.failure:
            raise SandboxConnectionError(
                f"sandbox memory snapshot failed: {snapshot.failure}",
                container_id=self.container_id,
                state=snapshot.state.value,
            )
        return str(snapshot.id)

    def create_image_from_filesystem(self) -> str:
        return self._call(self.client.create_filesystem_image).image_id

    def list_processes(self) -> list[SandboxProcessInfo]:
        response = _process_call(
            lambda: self.client.list_processes(UUID(self.container_id)), "processes"
        )
        return [
            SandboxProcessInfo(pid=item.pid, command=item.command) for item in response.processes
        ]

    def terminate(self) -> bool:
        if self.terminated:
            return True
        self._call(
            lambda container: self.client.api.stop_container(self.client.workspace, container)
        )
        self.terminated = True
        return True

    def _call(self, call: Callable[[UUID], T]) -> T:
        try:
            return call(UUID(self.container_id))
        except SdkError as exc:
            raise SandboxConnectionError(str(exc), container_id=self.container_id) from exc


@dataclass(slots=True)
class AsyncSandboxInstance:
    sandbox: SandboxInstance

    @property
    def fs(self) -> AsyncSandboxFileSystem:
        return self.sandbox.fs.aio

    @property
    def process(self) -> AsyncSandboxProcessManager:
        return self.sandbox.process.aio

    @property
    def docker(self) -> AsyncSandboxDockerManager:
        return self.sandbox.docker.aio

    async def run(
        self,
        command: str | Iterable[str],
        *,
        timeout_seconds: float | None = None,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> SandboxProcessResponse:
        return await to_thread(
            self.sandbox.run,
            command,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            env=env,
        )

    async def expose_port(self, port: int) -> str:
        return await to_thread(self.sandbox.expose_port, port)

    async def update_network_permissions(
        self,
        *,
        block_network: bool = False,
        allow_list: list[str] | None = None,
    ) -> SandboxNetworkPolicy:
        return await to_thread(
            self.sandbox.update_network_permissions,
            block_network=block_network,
            allow_list=allow_list,
        )

    async def network_permissions(self) -> SandboxNetworkPolicy:
        return await to_thread(self.sandbox.network_permissions)

    async def list_urls(self) -> dict[int, str]:
        return await to_thread(self.sandbox.list_urls)

    async def update_ttl(self, ttl: int) -> None:
        await to_thread(self.sandbox.update_ttl, ttl)

    async def snapshot_memory(self) -> str:
        return await to_thread(self.sandbox.snapshot_memory)

    async def create_image_from_filesystem(self) -> str:
        return await to_thread(self.sandbox.create_image_from_filesystem)

    async def list_processes(self) -> list[SandboxProcessInfo]:
        return await to_thread(self.sandbox.list_processes)

    async def terminate(self) -> bool:
        return await to_thread(self.sandbox.terminate)


@dataclass(init=False, slots=True)
class Sandbox(ControlClientConfigMixin):
    name: str = "sandbox"
    _app_slug: str = field(init=False, repr=False)
    image: Image = field(default_factory=Image)
    env: dict[str, str] | None = None
    command: list[str] = field(default_factory=lambda: ["tail", "-f", "/dev/null"])
    ports: list[int] = field(default_factory=list)
    cpu: CpuRequest | None = None
    memory: MemoryRequest | None = None
    disk: str | None = None
    gpu: GpuInput = None
    gpu_count: int = 0
    keep_warm_seconds: int = 600
    secrets: list[str] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    authorized: bool = False
    sync_local_dir: bool = False
    block_network: bool = False
    allow_list: list[str] | None = None
    docker_enabled: bool = False
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE
    region: str | None = None
    availability_zone: str = ""
    machine: MachineInput = None
    metadata: dict[str, Any] = field(default_factory=dict)
    stub_id: str = ""
    checkpoint_id: str | None = None
    client: WorkloadsClient | None = None
    workspace: str | None = None
    endpoint: str | None = None
    token: str | None = None
    timeout_seconds: float = SANDBOX_CONTROL_TIMEOUT_SECONDS
    ready_timeout_seconds: float = SANDBOX_READY_TIMEOUT_SECONDS
    terminal: Terminal | None = None

    def __init__(
        self,
        *,
        _app_slug: str,
        cpu: CpuRequest | str = 1.0,
        memory: MemoryRequest = 128,
        disk: str | None = None,
        gpu: GpuInput = None,
        gpu_count: int = 0,
        image: Image | None = None,
        keep_warm_seconds: int = 600,
        authorized: bool = False,
        name: str | None = None,
        volumes: Iterable[VolumeMount | VolumeExport] | None = None,
        secrets: Iterable[str] | None = None,
        env: Mapping[str, str] | None = None,
        sync_local_dir: bool = False,
        block_network: bool = False,
        allow_list: Iterable[str] | None = None,
        docker_enabled: bool = False,
        preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
        ports: Iterable[int] | None = None,
        region: str | None = None,
        availability_zone: str = "",
        machine: MachineInput = None,
        metadata: Mapping[str, Any] | None = None,
        command: Iterable[str] | None = None,
        timeout_seconds: float = SANDBOX_CONTROL_TIMEOUT_SECONDS,
        ready_timeout_seconds: float = SANDBOX_READY_TIMEOUT_SECONDS,
    ) -> None:
        self.name = name or "sandbox"
        self._app_slug = _app_slug
        self.image = image or Image()
        self.env = dict(env or {})
        self.command = [str(item) for item in (command or ["tail", "-f", "/dev/null"])]
        self.ports = _sandbox_ports(ports or [])
        self.cpu = _cpu_value(cpu)
        self.memory = _memory_value(memory)
        self.disk = disk
        self.gpu = gpu
        self.gpu_count = gpu_count
        self.keep_warm_seconds = keep_warm_seconds
        self.secrets = [str(secret) for secret in (secrets or [])]
        self.volumes = volume_mounts(volumes or [])
        self.authorized = authorized
        self.sync_local_dir = sync_local_dir
        self.block_network = block_network
        self.allow_list = list(allow_list) if allow_list is not None else None
        self.docker_enabled = docker_enabled
        self.preemptible = preemptible
        self.machine = machine
        self.region = region
        self.availability_zone = availability_zone
        self.metadata = dict(metadata or {})
        self.stub_id = ""
        self.checkpoint_id = None
        self.client = None
        self.workspace = None
        self.endpoint = None
        self.token = None
        self.timeout_seconds = _positive_timeout("timeout_seconds", timeout_seconds)
        self.ready_timeout_seconds = _nonnegative_timeout(
            "ready_timeout_seconds",
            ready_timeout_seconds,
        )
        self.terminal = None

    @property
    def control_client(self) -> WorkloadsClient:
        if self.client is None:
            self.client = workloads_client(self._config())
        return self.client

    @property
    def resource_name(self) -> str:
        return self.name

    def spec(self) -> DeploymentSpec:
        return DeploymentSpec(
            name=self.name,
            kind=DeploymentKind.Sandbox,
            image=self.image.spec(),
            resources=Resources(
                region=ProductRegion(self.region) if self.region is not None else None,
                availability_zone=self.availability_zone,
                cpu=self.cpu,
                memory=self.memory,
                disk=self.disk or DEFAULT_DISK,
                gpu=list(gpu_preference(self.gpu)),
                gpu_count=self.gpu_count,
                keep_warm=self.keep_warm_seconds,
                preemptible=self.preemptible,
            ),
            command=self.command,
            ports=_sandbox_port_mapping(self.ports),
            env=dict(self.env or {}),
            secrets=self.secrets,
            volumes=list(self.volumes),
            metadata=build_resource_metadata(
                app=self._app_slug,
                authorized=self.authorized,
                block_network=self.block_network,
                allow_list=self.allow_list,
                docker_enabled=self.docker_enabled,
                machine=self.machine,
                extra={
                    **self.metadata,
                    "sync_local_dir": self.sync_local_dir,
                },
            ),
        )

    def handler_reference(self) -> None:
        """A sandbox runs its command, not a handler."""
        return None

    def unsupported_options(self) -> builtins.list[str]:
        """Declared options the platform cannot run yet, by name."""
        from lazycloud.abstractions.pod import container_unsupported_options

        return container_unsupported_options(self.volumes, self.metadata)

    def require_supported(self) -> None:
        unsupported = self.unsupported_options()
        if unsupported:
            raise UnsupportedFeatureError(f"sandbox {self.name}", unsupported)

    def workload_spec(
        self, *, handler: object = None, source_sha256: str, image: ImageBuildResult
    ) -> WorkloadSpec:
        """The API definition of this sandbox for an uploaded source and a ready image."""
        from lazycloud.abstractions.pod import container_workload_spec

        del handler
        self.require_supported()
        pod = PodSpec(kind=PodKind.sandbox, command=[CommandItem(item) for item in self.command])
        if self.ports:
            pod.ports = _sandbox_port_mapping(self.ports)
        return container_workload_spec(
            self,
            label="sandbox",
            pod=pod,
            source_sha256=source_sha256,
            image=image,
            keep_warm=self.keep_warm_seconds,
            preemptible=self.preemptible,
            block_network=self.block_network,
            allow_list=self.allow_list,
        )

    def prepare(self, *, workspace: str | None = None) -> str:
        """Upload the sandbox's image and source and return the release it starts from.

        Without `sync_local_dir` the sandbox's /workspace starts empty.
        """
        from lazycloud.session.deployment import prepare_release

        if workspace is not None and workspace != self.workspace:
            self.workspace = workspace
            self.client = None
        client = self.control_client
        try:
            release = prepare_release(
                self, client=client.api, workspace=client.workspace, terminal=self.terminal
            )
        except SdkError as exc:
            raise SandboxConnectionError(str(exc)) from exc
        self.stub_id = str(release.id)
        return self.stub_id

    def create(
        self,
        *,
        stub_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SandboxInstance:
        if self.checkpoint_id:
            return self.create_from_memory_snapshot(
                self.checkpoint_id, timeout_seconds=timeout_seconds
            )
        selected_stub_id = stub_id or self.stub_id or self.prepare()
        response = self._create_instance(CreateInstanceRequest(release_id=UUID(selected_stub_id)))
        return self._instance_from_create_response(response, timeout_seconds=timeout_seconds)

    def create_from_memory_snapshot(
        self,
        snapshot_id: str,
        *,
        stub_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SandboxInstance:
        """Start a sandbox from a memory snapshot; it runs the release the snapshot was taken of."""
        del stub_id
        response = self._create_instance(CreateInstanceRequest(snapshot_id=UUID(snapshot_id)))
        return self._instance_from_create_response(response, timeout_seconds=timeout_seconds)

    def connect(
        self,
        sandbox_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SandboxInstance:
        stub_id = self._ready_stub_id(sandbox_id, timeout_seconds=timeout_seconds)
        return SandboxInstance(
            container_id=sandbox_id,
            stub_id=stub_id,
            client=self.control_client,
        )

    def list(
        self,
        *,
        workspace: str | None = None,
        app_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[SandboxRow]:
        """Sandbox containers, newest first, up to `limit`; `app_id` is an app's name."""
        client = self._client_for(workspace)
        rows: builtins.list[SandboxRow] = []
        cursor: str | None = None
        while len(rows) < limit:
            page = client.list_sandboxes(
                app=app_id, limit=min(1000, limit - len(rows)), cursor=cursor
            )
            rows.extend(page.sandboxes)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return rows

    def stats(
        self,
        *,
        workspace: str | None = None,
        app_id: str | None = None,
    ) -> SandboxStatsResponse:
        return self._client_for(workspace).sandbox_stats(app=app_id)

    def timeline(
        self,
        stub_id: str,
        *,
        workspace: str | None = None,
        container_id: str | None = None,
    ) -> SandboxTimeline:
        """When the release's newest sandbox, or `container_id`, was placed, ready and stopped."""
        client = self._client_for(workspace)
        selected = container_id or self._newest_container(client, stub_id)
        return _timeline(client.lifecycle(UUID(selected)))

    def _newest_container(self, client: WorkloadsClient, stub_id: str) -> str:
        cursor: str | None = None
        while True:
            page = client.list_sandboxes(app=self._app_slug, limit=1000, cursor=cursor)
            for row in page.sandboxes:
                if str(row.release_id) == stub_id:
                    return str(row.id)
            if page.next_cursor is None:
                raise SandboxConnectionError(f"sandbox release {stub_id} has no container")
            cursor = page.next_cursor

    def _client_for(self, workspace: str | None) -> WorkloadsClient:
        if workspace is None or workspace == self.control_client.workspace:
            return self.control_client
        return WorkloadsClient(self.control_client.api, workspace)

    def _create_instance(self, request: CreateInstanceRequest) -> Instance:
        try:
            return self.control_client.create_instance(request)
        except SdkError as exc:
            raise SandboxConnectionError(f"failed to create sandbox: {exc}") from exc

    def _instance_from_create_response(
        self,
        response: Instance,
        *,
        timeout_seconds: float | None,
    ) -> SandboxInstance:
        container_id = str(response.id)
        try:
            ready_stub_id = self._ready_stub_id(container_id, timeout_seconds=timeout_seconds)
        except SandboxConnectionError as exc:
            self._raise_failed_create(container_id, exc)
        except (KeyboardInterrupt, SystemExit) as exc:
            self._raise_interrupted_create(container_id, exc)
        instance = SandboxInstance(
            container_id=container_id,
            stub_id=ready_stub_id or str(response.release_id),
            client=self.control_client,
        )
        try:
            for port in self.ports:
                instance.expose_port(port)
        except (OSError, RuntimeError) as exc:
            self._raise_failed_create(
                instance.container_id,
                SandboxConnectionError(
                    f"sandbox {instance.container_id} port exposure failed: {exc}",
                    container_id=instance.container_id,
                    state=str(exc),
                ),
            )
        except (KeyboardInterrupt, SystemExit) as exc:
            self._raise_interrupted_create(container_id, exc)
        return instance

    def _ready_stub_id(
        self,
        container_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        ready_timeout_seconds = (
            self.ready_timeout_seconds
            if timeout_seconds is None
            else _nonnegative_timeout("timeout_seconds", timeout_seconds)
        )
        deadline = time.monotonic() + ready_timeout_seconds
        container = UUID(container_id)
        while True:
            remaining = deadline - time.monotonic()
            try:
                response = self.control_client.connect(
                    container, wait_seconds=max(0, int(min(CONNECT_WAIT_SECONDS, remaining)))
                )
            except ApiError as exc:
                state = exc.message
                if exc.code is not ErrorCode.unavailable:
                    raise SandboxConnectionError(
                        f"sandbox {container_id} readiness failed: {state}",
                        container_id=container_id,
                        state=state,
                    ) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxConnectionError(
                        f"sandbox {container_id} did not become ready within "
                        f"{ready_timeout_seconds:g} seconds; last state: {state}",
                        container_id=container_id,
                        state=state,
                    ) from exc
                time.sleep(min(SANDBOX_WAIT_POLL_INTERVAL_SECONDS, remaining))
                continue
            except (OSError, RuntimeError) as exc:
                state = f"{type(exc).__name__}: {exc}"
                raise SandboxConnectionError(
                    f"sandbox {container_id} readiness transport failed: {state}",
                    container_id=container_id,
                    state=state,
                ) from exc
            return str(response.release_id)

    def _terminate(self, container_id: str) -> None:
        client = self.control_client
        client.api.stop_container(client.workspace, UUID(container_id))

    def _raise_interrupted_create(self, container_id: str, interruption: BaseException) -> Never:
        try:
            self._terminate(container_id)
        except Exception as cleanup_error:
            raise interruption from cleanup_error
        raise interruption

    def _raise_failed_create(
        self,
        container_id: str,
        readiness_error: SandboxConnectionError,
    ) -> Never:
        try:
            self._terminate(container_id)
        except (OSError, RuntimeError) as cleanup_error:
            raise SandboxConnectionError(
                f"{readiness_error}; failed to terminate sandbox {container_id}: "
                f"{type(cleanup_error).__name__}: {cleanup_error}",
                container_id=container_id,
                state=readiness_error.state,
                cleanup_error=cleanup_error,
            ) from readiness_error
        raise readiness_error


def _command_args_list(command: str | Iterable[str]) -> list[str]:
    args = shlex.split(command) if isinstance(command, str) else [str(item) for item in command]
    if not args:
        msg = "command cannot be empty"
        raise SandboxProcessError(msg)
    return args


def _process_call(call: Callable[[], T], label: str) -> T:
    """Run a process request with transient retries; API failures raise SandboxProcessError."""
    try:
        return retry_transient(call, not_found=lambda: SandboxProcessError(f"{label} not found"))
    except SandboxProcessError:
        raise
    except SdkError as exc:
        raise SandboxProcessError(str(exc)) from exc


def _exit_code(process: Process) -> int:
    return process.exit_code if process.exit_code is not None else -1


def _network_policy(policy: NetworkPolicy) -> SandboxNetworkPolicy:
    return SandboxNetworkPolicy(
        block_network=policy.block_network,
        allow_list=[item.root for item in policy.allow_list],
    )


def _timeline(lifecycle: ContainerLifecycle) -> SandboxTimeline:
    return SandboxTimeline(
        container_id=str(lifecycle.container_id),
        status=_sandbox_status(lifecycle),
        created_at=lifecycle.created_at,
        scheduled_at=lifecycle.assigned_at,
        started_at=lifecycle.ready_at,
        ended_at=lifecycle.stopped_at,
        scheduling_ms=_duration_ms(lifecycle.created_at, lifecycle.assigned_at),
        startup_ms=_duration_ms(lifecycle.created_at, lifecycle.ready_at),
        runtime_ms=_duration_ms(lifecycle.ready_at, lifecycle.stopped_at),
    )


def _sandbox_status(lifecycle: ContainerLifecycle) -> SandboxStatus:
    if lifecycle.state is ContainerState.stopped:
        if lifecycle.stop_reason in _FAILED_STOP_REASONS:
            return SandboxStatus.failed
        return SandboxStatus.stopped
    if lifecycle.state is ContainerState.ready:
        return SandboxStatus.running
    if lifecycle.state is ContainerState.draining:
        return SandboxStatus.stopping
    return SandboxStatus.pending


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _cpu_value(value: CpuRequest | str) -> CpuRequest:
    """Cores as a number, keeping a `(reserve, throttle at)` pair as a pair."""
    request, limit = request_and_limit(value)
    if limit is None:
        return _cores(request)
    return (_cores(request), _cores(limit))


def _cores(value: str | int | float | None) -> float:
    if value is None:
        msg = "sandbox cpu value is required"
        raise TypeError(msg)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        msg = f"unsupported sandbox cpu value: {value!r}"
        raise TypeError(msg) from exc


def _positive_timeout(name: str, value: float) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero")
    return timeout


def _nonnegative_timeout(name: str, value: float) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return timeout


def _memory_value(value: MemoryRequest) -> MemoryRequest:
    """Memory as written, keeping a `(reserve, kill at)` pair as a pair."""
    request, limit = request_and_limit(value)
    if limit is None:
        return str(request)
    return (str(request), str(limit))


def _sandbox_ports(ports: Iterable[int]) -> list[int]:
    validated: list[int] = []
    for port in ports:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("sandbox ports must be integers between 1 and 65535")
        validated.append(port)
    return validated


def _sandbox_port_mapping(ports: Iterable[int]) -> dict[str, int]:
    return {str(port): int(port) for port in ports}


def _path_text(path: str | Path) -> str:
    text = str(path)
    return text or "."


def _join_remote_path(parent: str, name: str) -> str:
    if parent in {"", "."}:
        return name
    return f"{parent.rstrip('/')}/{name}"


def _command_args(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        return ["sh", "-lc", command]
    return [str(item) for item in command]


def _docker_env_args(env: Mapping[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in env.items():
        args.extend(["--env", f"{key}={value}"])
    return args


def _docker_volume_args(volumes: Mapping[str, str] | Sequence[str]) -> list[str]:
    args: list[str] = []
    if isinstance(volumes, Mapping):
        for host, container in volumes.items():
            args.extend(["--volume", f"{host}:{container}"])
        return args
    for volume in volumes:
        args.extend(["--volume", str(volume)])
    return args


def _remote_path(path: str, *, cwd: str) -> str:
    if path.startswith("/"):
        return path
    return _join_remote_path(cwd, path)


def _compose_build_override(build: JsonValue) -> JsonValue:
    if isinstance(build, dict):
        return {**build, "network": "host"}
    if isinstance(build, str):
        return {"context": build, "network": "host"}
    return {"network": "host"}


def _sandbox_file_info(file_info: ContainerFile, *, requested_path: str) -> SandboxFileInfo:
    return SandboxFileInfo(
        name=file_info.name or Path(requested_path).name,
        is_dir=file_info.is_dir,
        size=file_info.size,
        mode=file_info.mode,
        mod_time=file_info.mod_time,
        owner=file_info.owner,
        group=file_info.group,
        permissions=file_info.permissions,
    )


def _sandbox_search_match(match: FileMatch, text: str) -> SandboxFileSearchMatch:
    start = SandboxFilePosition(line=match.line, column=match.column)
    end = SandboxFilePosition(line=match.line, column=match.column + len(text))
    return SandboxFileSearchMatch(
        range=SandboxFileSearchRange(start=start, end=end),
        content=match.text or text,
    )


def _sandbox_search_results(
    matches: Iterable[FileMatch],
    pattern: str,
) -> list[SandboxFileSearchResult]:
    by_path: dict[str, list[SandboxFileSearchMatch]] = {}
    for match in matches:
        by_path.setdefault(match.path, []).append(_sandbox_search_match(match, pattern))
    return [SandboxFileSearchResult(path=path, matches=items) for path, items in by_path.items()]


__all__ = [
    "AsyncSandboxDockerManager",
    "AsyncSandboxFileSystem",
    "AsyncSandboxInstance",
    "AsyncSandboxProcess",
    "AsyncSandboxProcessManager",
    "DockerComposeStack",
    "DockerResult",
    "Sandbox",
    "SandboxConnectionError",
    "SandboxDockerManager",
    "SandboxFileInfo",
    "SandboxFilePosition",
    "SandboxFileSearchMatch",
    "SandboxFileSearchRange",
    "SandboxFileSearchResult",
    "SandboxFileSystem",
    "SandboxFileSystemError",
    "SandboxInstance",
    "SandboxOptions",
    "SandboxProcess",
    "SandboxProcessError",
    "SandboxProcessInfo",
    "SandboxProcessManager",
    "SandboxProcessResponse",
    "SandboxProcessStream",
    "SandboxProcessTimeoutError",
    "SandboxRow",
    "SandboxStatsResponse",
    "SandboxTimeline",
]
