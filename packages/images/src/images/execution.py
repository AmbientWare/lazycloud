from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Protocol

from pydantic import Field
from shared.contracts import ContractModel
from shared.image_building.planning import ImageBuildPlan
from shared.image_building.records import BuildStatus

from images.building import (
    ImageBuildCredentialPlan,
    ImageBuildSessionPlan,
    ImageBuildStreamEventPlan,
    plan_image_build_complete_event,
    plan_image_build_log_event,
    plan_image_build_wait_event,
    plan_image_build_wait_probe,
)


class ImageBuildExecutorKind(StrEnum):
    Manifest = "manifest"
    LocalDocker = "local-docker"
    BuildContainer = "build-container"


class ImageBuildProcessResult(ContractModel):
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    reason: str = ""


type ImageBuildCommandRunner = Callable[
    [Sequence[str], Path, int | None],
    ImageBuildProcessResult,
]
type ImageBuildEventSink = Callable[[ImageBuildStreamEventPlan], None]


class ImageBuildExecutionRequest(ContractModel):
    build_id: str
    workspace_id: str = ""
    image_id: str
    tag: str = ""
    build_dir: str
    dockerfile_path: str
    manifest_path: str
    plan: ImageBuildPlan
    session: ImageBuildSessionPlan
    credential_plan: ImageBuildCredentialPlan | None = None
    registry_credential_payload: str = Field(default="", repr=False)
    build_args: dict[str, str] = Field(default_factory=dict, repr=False)
    event_sink: ImageBuildEventSink | None = Field(default=None, exclude=True, repr=False)


class ImageBuildExecutionResult(ContractModel):
    status: BuildStatus
    events: list[ImageBuildStreamEventPlan] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    published_ref: str = ""
    artifact_path: str = ""
    cache_metadata: dict[str, str] = Field(default_factory=dict)
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is BuildStatus.Complete


class ImageBuildExecutor(Protocol):
    cache_markers: ClassVar[frozenset[str]]
    requires_archive_publication: ClassVar[bool]

    def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult: ...


def emit_image_build_event(
    request: ImageBuildExecutionRequest,
    event: ImageBuildStreamEventPlan,
) -> ImageBuildStreamEventPlan:
    if request.event_sink is not None:
        request.event_sink(event)
    return event


def create_image_build_executor(
    kind: ImageBuildExecutorKind | str,
    *,
    docker_binary: str = "docker",
) -> ImageBuildExecutor:
    selected = kind if isinstance(kind, ImageBuildExecutorKind) else ImageBuildExecutorKind(kind)
    if selected is ImageBuildExecutorKind.Manifest:
        return ManifestImageBuildExecutor()
    if selected is ImageBuildExecutorKind.LocalDocker:
        return LocalDockerImageBuildExecutor(docker_binary=docker_binary)
    if selected is ImageBuildExecutorKind.BuildContainer:
        msg = "build-container image executor requires scheduler and container service dependencies"
        raise ValueError(msg)
    msg = f"unsupported image build executor kind: {selected}"
    raise ValueError(msg)


@dataclass(slots=True)
class ManifestImageBuildExecutor:
    """Completes builds whose durable manifest is the runtime artifact."""

    cache_markers: ClassVar[frozenset[str]] = frozenset({ImageBuildExecutorKind.Manifest.value})
    requires_archive_publication: ClassVar[bool] = False

    def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
        event = plan_image_build_complete_event(
            image_id=request.image_id,
            build_id=request.build_id,
            python_version=request.plan.spec.python_version,
        )
        return ImageBuildExecutionResult(
            status=BuildStatus.Complete,
            events=[event],
            published_ref=request.tag or request.image_id,
            artifact_path=request.manifest_path,
            cache_metadata={
                "executor": ImageBuildExecutorKind.Manifest.value,
                "manifest_path": request.manifest_path,
                "dockerfile_path": request.dockerfile_path,
            },
            reason="manifest build artifact is ready",
        )


@dataclass(slots=True)
class LocalDockerImageBuildExecutor:
    cache_markers: ClassVar[frozenset[str]] = frozenset({ImageBuildExecutorKind.LocalDocker.value})
    requires_archive_publication: ClassVar[bool] = False

    docker_binary: str = "docker"
    runner: ImageBuildCommandRunner | None = None

    def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
        command = self.build_command(request)
        logged_command = self.build_command(request, redact_secret_values=True)
        timeout_seconds = (
            request.session.spinup_timeout.timeout_seconds
            if request.session.spinup_timeout is not None
            else None
        )
        events = [
            plan_image_build_log_event(
                f"docker build command: {shlex.join(logged_command)}",
                image_id=request.image_id,
                build_id=request.build_id,
                python_version=request.plan.spec.python_version,
            )
        ]
        process = self._runner(command, Path(request.build_dir), timeout_seconds)
        events.extend(_process_output_events(request, process))

        if process.timed_out:
            wait = plan_image_build_wait_probe(
                clip_version=request.session.clip_version,
                timed_out=True,
            )
        elif process.exit_code is None:
            wait = plan_image_build_wait_probe(
                clip_version=request.session.clip_version,
                exit_code=1,
            )
        else:
            wait = plan_image_build_wait_probe(
                clip_version=request.session.clip_version,
                exit_code=process.exit_code,
            )

        terminal_event = plan_image_build_wait_event(
            wait,
            image_id=request.image_id,
            build_id=request.build_id,
            python_version=request.plan.spec.python_version,
        )
        events.append(terminal_event)
        return ImageBuildExecutionResult(
            status=terminal_event.status,
            events=events,
            command=list(logged_command),
            exit_code=process.exit_code,
            published_ref=(request.tag or request.image_id)
            if terminal_event.status is BuildStatus.Complete
            else "",
            artifact_path=str(Path(request.build_dir))
            if terminal_event.status is BuildStatus.Complete
            else "",
            cache_metadata=_cache_metadata_for_docker_build(request, logged_command)
            if terminal_event.status is BuildStatus.Complete
            else {},
            reason=terminal_event.error or terminal_event.message.strip() or process.reason,
        )

    def build_command(
        self,
        request: ImageBuildExecutionRequest,
        *,
        redact_secret_values: bool = False,
    ) -> list[str]:
        context_path = _context_path(request)
        tag = request.tag or request.image_id
        command = [
            self.docker_binary,
            "build",
            "--file",
            request.dockerfile_path,
            "--tag",
            tag,
        ]
        for name, value in sorted(request.build_args.items()):
            if not name or not value:
                continue
            rendered_value = "<redacted>" if redact_secret_values else value
            command.extend(["--build-arg", f"{name}={rendered_value}"])
        command.append(str(context_path))
        return command

    def _runner(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: int | None,
    ) -> ImageBuildProcessResult:
        runner = self.runner or run_image_build_command
        return runner(command, cwd, timeout_seconds)


def run_image_build_command(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int | None,
) -> ImageBuildProcessResult:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        return ImageBuildProcessResult(exit_code=127, stderr=str(exc), reason="builder not found")
    except subprocess.TimeoutExpired as exc:
        return ImageBuildProcessResult(
            exit_code=None,
            stdout=_decode_output(exc.stdout),
            stderr=_decode_output(exc.stderr),
            timed_out=True,
            reason="builder timed out",
        )
    return ImageBuildProcessResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _process_output_events(
    request: ImageBuildExecutionRequest,
    process: ImageBuildProcessResult,
) -> list[ImageBuildStreamEventPlan]:
    events: list[ImageBuildStreamEventPlan] = []
    for line in _combined_output_lines(process):
        events.append(
            plan_image_build_log_event(
                line,
                image_id=request.image_id,
                build_id=request.build_id,
                python_version=request.plan.spec.python_version,
            )
        )
    if process.reason and not events:
        events.append(
            plan_image_build_log_event(
                process.reason,
                image_id=request.image_id,
                build_id=request.build_id,
                python_version=request.plan.spec.python_version,
                warning=True,
            )
        )
    return events


def _combined_output_lines(process: ImageBuildProcessResult) -> list[str]:
    output = "\n".join(value for value in (process.stdout, process.stderr) if value)
    return [line for line in output.splitlines() if line.strip()]


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _context_path(request: ImageBuildExecutionRequest) -> Path:
    if request.plan.spec.context_path:
        return Path(request.plan.spec.context_path)
    return Path(request.build_dir)


def _cache_metadata_for_docker_build(
    request: ImageBuildExecutionRequest,
    logged_command: Sequence[str],
) -> dict[str, str]:
    return {
        "executor": ImageBuildExecutorKind.LocalDocker.value,
        "command": shlex.join(logged_command),
        "tag": request.tag or request.image_id,
        "manifest_path": request.manifest_path,
        "dockerfile_path": request.dockerfile_path,
    }
