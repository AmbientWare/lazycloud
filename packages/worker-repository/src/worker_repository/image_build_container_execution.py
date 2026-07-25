"""Control-plane orchestration for worker-hosted image-build containers."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from foundation.io_utils import OutputMessage
from images.building import (
    ImageBuildStreamEventPlan,
    ImageBuildWaitOutcome,
    ImageBuildWaitPlan,
    ImageInstallCommandMode,
    plan_image_build_commands,
    plan_image_build_failure_event,
    plan_image_build_log_event,
    plan_image_build_wait_event,
    plan_image_build_wait_probe,
)
from images.execution import (
    ImageBuildExecutionRequest,
    ImageBuildExecutionResult,
    emit_image_build_event,
)
from images.log_streaming import ImageBuildLogStreamCollector
from images.scheduling import (
    ImageBuildSchedulingFailureEvidence,
    image_build_scheduling_failure,
)
from shared.contracts import ContractModel
from shared.image_building.records import BuildStatus
from shared.scheduling import SchedulerContainerAddress, SchedulerContainerState
from worker.container_client.control import (
    ContainerArchiveError,
    ContainerClientStreamError,
    ContainerServiceClient,
    ContainerServiceTransport,
    plan_container_client_connection_options,
)
from worker.container_client.models import (
    ContainerClientConnectionOptions,
    ContainerExecResponse,
    ContainerKillResponse,
    ContainerStatusResponse,
)

DEFAULT_IMAGE_BUILD_CONTAINER_ENV = (
    "DEBIAN_FRONTEND=noninteractive",
    "PIP_ROOT_USER_ACTION=ignore",
    "UV_NO_CACHE=true",
    "UV_COMPILE_BYTECODE=true",
)
PRIVATE_INPUTS_REQUIRE_V2_REASON = (
    "private image build inputs require the worker-local v2 build path"
)


class ImageBuildContainerStatusKind(StrEnum):
    Running = "running"
    Complete = "complete"
    Failed = "failed"
    Unknown = "unknown"


class ImageBuildContainerCommandStatus(StrEnum):
    Complete = "complete"
    Failed = "failed"
    Skipped = "skipped"


class ImageBuildContainerExecutorFactoryStatus(StrEnum):
    Ready = "ready"
    MissingAddress = "missing-address"
    SchedulingFailed = "scheduling-failed"


class ImageBuildContainerCommandResult(ContractModel):
    status: ImageBuildContainerCommandStatus
    command: str = ""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is not ImageBuildContainerCommandStatus.Failed


class ImageBuildContainerRuntimeClient(Protocol):
    def status(self, container_id: str) -> ContainerStatusResponse: ...

    def exec(
        self,
        container_id: str,
        command: str,
        env: Sequence[str] = (),
    ) -> ContainerExecResponse: ...

    def archive(
        self,
        container_id: str,
        image_id: str,
        output: Callable[[OutputMessage], None],
    ) -> None: ...

    def stream_logs(
        self,
        container_id: str,
        output: Callable[[OutputMessage], None],
    ) -> None: ...

    def kill(self, container_id: str) -> ContainerKillResponse: ...


class ImageBuildContainerAddressResolver(Protocol):
    def worker_address(self, container_id: str) -> SchedulerContainerAddress | None: ...

    def scheduling_failure(
        self,
        container_id: str,
    ) -> ImageBuildSchedulingFailureEvidence | None: ...


class ImageBuildContainerAddressRepository(Protocol):
    def get_worker_address(self, container_id: str) -> SchedulerContainerAddress | None: ...

    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...


class ImageBuildContainerServiceTokenProvider(Protocol):
    def token_for_container(self, container_id: str) -> str: ...


class ContainerServiceTransportFactory(Protocol):
    def create_transport(
        self,
        options: ContainerClientConnectionOptions,
    ) -> ContainerServiceTransport: ...


@dataclass(slots=True)
class ImageBuildContainerExecutorFactoryResult:
    status: ImageBuildContainerExecutorFactoryStatus
    container_id: str
    service_url: str = ""
    options: ContainerClientConnectionOptions | None = None
    executor: ContainerServiceImageBuildExecutor | None = None
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.status is ImageBuildContainerExecutorFactoryStatus.Ready


@dataclass(slots=True)
class ContainerServiceImageBuildExecutor:
    client: ImageBuildContainerRuntimeClient
    poll_interval_seconds: float = 0.1
    log_stream_drain_wait_seconds: float = 1.0
    last_archive_object_id: str = ""
    last_archive_object_key: str = ""
    last_archive_size_bytes: int = 0
    last_archive_sha256: str = ""

    def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
        events: list[ImageBuildStreamEventPlan] = [
            _event(
                request,
                plan_image_build_log_event(
                    "build container execution started",
                    image_id=request.image_id,
                    build_id=request.build_id,
                    python_version=request.plan.spec.python_version,
                ),
            )
        ]
        if not request.session.build_container_required:
            complete = plan_image_build_wait_event(
                plan_image_build_wait_probe(clip_version=request.session.clip_version, exit_code=0),
                image_id=request.image_id,
                build_id=request.build_id,
                python_version=request.plan.spec.python_version,
            )
            events.append(_event(request, complete))
            return _execution_result(request, complete.status, events, reason=complete.message)

        log_stream = ImageBuildLogStreamCollector(
            self.client,
            request.session.container_id,
            lambda message: _output_event(request, message),
        ).start()
        try:
            wait = self.wait_for_container(request)
            wait_event = plan_image_build_wait_event(
                wait,
                image_id=request.image_id,
                build_id=request.build_id,
                python_version=request.plan.spec.python_version,
            )
            events.append(_event(request, wait_event))
            events.extend(_drain_log_stream(request, log_stream))
            if request.session.v2 or (
                wait.terminal
                and wait.status is not BuildStatus.Complete
                and wait.outcome is not ImageBuildWaitOutcome.Running
            ):
                result = _execution_result(
                    request,
                    wait.status,
                    events,
                    reason=wait.reason,
                    staging_archive_object_id=self.last_archive_object_id,
                    staging_archive_object_key=self.last_archive_object_key,
                    staging_archive_size_bytes=self.last_archive_size_bytes,
                    staging_archive_sha256=self.last_archive_sha256,
                )
            else:
                result = self._execute_v1(request, events, log_stream)
        finally:
            cleanup_events = self._close_execution(request, log_stream)
        return result.model_copy(update={"events": [*result.events, *cleanup_events]})

    def _execute_v1(
        self,
        request: ImageBuildExecutionRequest,
        events: list[ImageBuildStreamEventPlan],
        log_stream: ImageBuildLogStreamCollector,
    ) -> ImageBuildExecutionResult:
        command_results = self.execute_runtime_commands(request)
        for result in command_results:
            events.extend(_command_events(request, result))
            events.extend(_drain_log_stream(request, log_stream))
            if not result.complete:
                return _execution_result(
                    request,
                    BuildStatus.Failed,
                    events,
                    reason=result.reason,
                )

        archive_events: list[ImageBuildStreamEventPlan] = []
        try:
            self.client.archive(
                request.session.container_id,
                request.image_id,
                lambda message: archive_events.append(_output_event(request, message)),
            )
            self._capture_archive_identity(self.client.status(request.session.container_id))
        except (ContainerArchiveError, ContainerClientStreamError) as exc:
            events.extend(archive_events)
            events.append(
                _event(
                    request,
                    plan_image_build_failure_event(
                        str(exc),
                        image_id=request.image_id,
                        build_id=request.build_id,
                        python_version=request.plan.spec.python_version,
                    ),
                )
            )
            return _execution_result(request, BuildStatus.Failed, events, reason=str(exc))

        events.extend(event for event in archive_events if event.message.strip())
        events.extend(_drain_log_stream(request, log_stream))
        complete = plan_image_build_wait_event(
            plan_image_build_wait_probe(clip_version=request.session.clip_version, exit_code=0),
            image_id=request.image_id,
            build_id=request.build_id,
            python_version=request.plan.spec.python_version,
        )
        events.append(_event(request, complete))
        return _execution_result(
            request,
            BuildStatus.Complete,
            events,
            reason=complete.message,
            staging_archive_object_id=self.last_archive_object_id,
            staging_archive_object_key=self.last_archive_object_key,
            staging_archive_size_bytes=self.last_archive_size_bytes,
            staging_archive_sha256=self.last_archive_sha256,
        )

    def wait_for_container(self, request: ImageBuildExecutionRequest) -> ImageBuildWaitPlan:
        timeout_seconds = (
            request.session.spinup_timeout.timeout_seconds
            if request.session.spinup_timeout is not None
            else 0
        )
        deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
        while True:
            response = self.client.status(request.session.container_id)
            self._capture_archive_identity(response)
            wait = _wait_probe_from_status(response, clip_version=request.session.clip_version)
            if wait.outcome is not ImageBuildWaitOutcome.Continue:
                return wait
            if deadline is not None and time.monotonic() >= deadline:
                return plan_image_build_wait_probe(
                    clip_version=request.session.clip_version,
                    timed_out=True,
                )
            time.sleep(max(self.poll_interval_seconds, 0.0))

    def _capture_archive_identity(self, response: ContainerStatusResponse) -> None:
        if response.build_archive_object_id:
            self.last_archive_object_id = response.build_archive_object_id
        if response.build_archive_object_key:
            self.last_archive_object_key = response.build_archive_object_key
        if response.build_archive_size_bytes > 0:
            self.last_archive_size_bytes = response.build_archive_size_bytes
        if response.build_archive_sha256:
            self.last_archive_sha256 = response.build_archive_sha256

    def execute_runtime_commands(
        self,
        request: ImageBuildExecutionRequest,
    ) -> list[ImageBuildContainerCommandResult]:
        if request.build_args or request.registry_credential_payload:
            return [
                ImageBuildContainerCommandResult(
                    status=ImageBuildContainerCommandStatus.Failed,
                    reason=PRIVATE_INPUTS_REQUIRE_V2_REASON,
                )
            ]
        commands = plan_image_build_commands(
            request.plan.spec,
            mode=ImageInstallCommandMode.Runtime,
            python_executable=request.plan.spec.python_version or "python",
            system=True,
        )
        if not commands:
            return [
                ImageBuildContainerCommandResult(
                    status=ImageBuildContainerCommandStatus.Skipped,
                    reason="no runtime image build commands",
                )
            ]
        results: list[ImageBuildContainerCommandResult] = []
        for command in commands:
            response = self.client.exec(
                request.session.container_id,
                command.command,
                env=DEFAULT_IMAGE_BUILD_CONTAINER_ENV,
            )
            status = (
                ImageBuildContainerCommandStatus.Complete
                if response.ok
                else ImageBuildContainerCommandStatus.Failed
            )
            results.append(
                ImageBuildContainerCommandResult(
                    status=status,
                    command=command.command,
                    exit_code=response.exit_code,
                    stdout=response.stdout,
                    stderr=response.stderr,
                    reason=response.error_msg or response.stderr,
                )
            )
            if status is ImageBuildContainerCommandStatus.Failed:
                break
        return results

    def _close_execution(
        self,
        request: ImageBuildExecutionRequest,
        log_stream: ImageBuildLogStreamCollector,
    ) -> list[ImageBuildStreamEventPlan]:
        cleanup_error = self._kill(request.session.container_id)
        log_stream.close(self.log_stream_drain_wait_seconds)
        events = _drain_log_stream(request, log_stream)
        if cleanup_error:
            events.append(
                _event(
                    request,
                    plan_image_build_log_event(
                        cleanup_error,
                        image_id=request.image_id,
                        build_id=request.build_id,
                        python_version=request.plan.spec.python_version,
                    ),
                )
            )
        return events

    def _kill(self, container_id: str) -> str:
        try:
            response = self.client.kill(container_id)
        except Exception as exc:
            return f"build container shutdown failed ({type(exc).__name__})"
        if not response.ok:
            return response.error_msg.strip() or "build container shutdown failed"
        return ""


@dataclass(slots=True)
class ContainerServiceImageBuildExecutorFactory:
    address_resolver: ImageBuildContainerAddressResolver
    transport_factory: ContainerServiceTransportFactory
    token_provider: ImageBuildContainerServiceTokenProvider | None = None
    poll_interval_seconds: float = 0.1

    def create(self, container_id: str) -> ImageBuildContainerExecutorFactoryResult:
        address = self.address_resolver.worker_address(container_id)
        if address is None or not address.address:
            failure = self.address_resolver.scheduling_failure(container_id)
            if failure is not None:
                return ImageBuildContainerExecutorFactoryResult(
                    status=ImageBuildContainerExecutorFactoryStatus.SchedulingFailed,
                    container_id=container_id,
                    reason=failure.reason,
                )
            return ImageBuildContainerExecutorFactoryResult(
                status=ImageBuildContainerExecutorFactoryStatus.MissingAddress,
                container_id=container_id,
                reason=f"worker address not found for build container: {container_id}",
            )
        token = (
            self.token_provider.token_for_container(container_id)
            if self.token_provider is not None
            else ""
        )
        options = plan_container_client_connection_options(
            address.address,
            token,
            backend_route_id=address.route.route_id if address.route is not None else "",
        )
        transport = self.transport_factory.create_transport(options)
        return ImageBuildContainerExecutorFactoryResult(
            status=ImageBuildContainerExecutorFactoryStatus.Ready,
            container_id=container_id,
            service_url=address.address,
            options=options,
            executor=ContainerServiceImageBuildExecutor(
                ContainerServiceClient(transport),
                poll_interval_seconds=self.poll_interval_seconds,
            ),
            reason="container service image build executor ready",
        )


@dataclass(slots=True)
class SchedulerImageBuildContainerAddressResolver:
    repository: ImageBuildContainerAddressRepository

    def worker_address(self, container_id: str) -> SchedulerContainerAddress | None:
        return self.repository.get_worker_address(container_id)

    def scheduling_failure(
        self,
        container_id: str,
    ) -> ImageBuildSchedulingFailureEvidence | None:
        return image_build_scheduling_failure(self.repository.get_container_state(container_id))


@dataclass(slots=True)
class StaticImageBuildContainerServiceTokenProvider:
    token: str = ""

    def token_for_container(self, container_id: str) -> str:
        del container_id
        return self.token


def _wait_probe_from_status(
    response: ContainerStatusResponse,
    *,
    clip_version: int,
) -> ImageBuildWaitPlan:
    status = _container_status_kind(response)
    if response.exit_code != 0:
        return plan_image_build_wait_probe(
            clip_version=clip_version,
            exit_code=response.exit_code,
            failure_reason=response.error_msg,
        )
    if clip_version >= 2 and status is ImageBuildContainerStatusKind.Complete:
        return plan_image_build_wait_probe(clip_version=clip_version, exit_code=0)
    if clip_version >= 2 and status is ImageBuildContainerStatusKind.Failed:
        return plan_image_build_wait_probe(
            clip_version=clip_version,
            exit_code=1,
            failure_reason=response.error_msg,
        )
    if clip_version < 2 and status is ImageBuildContainerStatusKind.Running:
        return plan_image_build_wait_probe(clip_version=clip_version, container_running=True)
    if clip_version < 2 and status is ImageBuildContainerStatusKind.Failed:
        return plan_image_build_wait_probe(
            clip_version=clip_version,
            exit_code=1,
            failure_reason=response.error_msg,
        )
    return plan_image_build_wait_probe(clip_version=clip_version)


def _container_status_kind(response: ContainerStatusResponse) -> ImageBuildContainerStatusKind:
    if not response.ok:
        return ImageBuildContainerStatusKind.Failed
    value = response.status.strip().lower()
    if value in {"running", "started"}:
        return ImageBuildContainerStatusKind.Running
    if value in {"complete", "completed", "exited", "succeeded", "success"}:
        return ImageBuildContainerStatusKind.Complete
    if value in {"failed", "error", "stopped"}:
        return ImageBuildContainerStatusKind.Failed
    return ImageBuildContainerStatusKind.Unknown


def _command_events(
    request: ImageBuildExecutionRequest,
    result: ImageBuildContainerCommandResult,
) -> list[ImageBuildStreamEventPlan]:
    if result.status is ImageBuildContainerCommandStatus.Skipped:
        return []
    events = [
        _event(
            request,
            plan_image_build_log_event(
                f"running: {result.command}",
                image_id=request.image_id,
                build_id=request.build_id,
                python_version=request.plan.spec.python_version,
            ),
        )
    ]
    for line in _output_lines(result.stdout, result.stderr):
        events.append(
            _event(
                request,
                plan_image_build_log_event(
                    line,
                    image_id=request.image_id,
                    build_id=request.build_id,
                    python_version=request.plan.spec.python_version,
                ),
            )
        )
    if not result.complete:
        events.append(
            _event(
                request,
                plan_image_build_failure_event(
                    result.reason or "runtime image build command failed",
                    image_id=request.image_id,
                    build_id=request.build_id,
                    python_version=request.plan.spec.python_version,
                ),
            )
        )
    return events


def _output_event(
    request: ImageBuildExecutionRequest,
    message: OutputMessage,
) -> ImageBuildStreamEventPlan:
    return _event(
        request,
        plan_image_build_log_event(
            message.msg,
            image_id=request.image_id,
            build_id=request.build_id,
            python_version=request.plan.spec.python_version,
        ),
    )


def _drain_log_stream(
    request: ImageBuildExecutionRequest,
    collector: ImageBuildLogStreamCollector,
) -> list[ImageBuildStreamEventPlan]:
    drained = collector.drain()
    events = list(drained.events)
    for error in drained.errors:
        events.append(
            _event(
                request,
                plan_image_build_log_event(
                    f"build container log stream failed: {error}",
                    image_id=request.image_id,
                    build_id=request.build_id,
                    python_version=request.plan.spec.python_version,
                ),
            )
        )
    return events


def _event(
    request: ImageBuildExecutionRequest,
    event: ImageBuildStreamEventPlan,
) -> ImageBuildStreamEventPlan:
    return emit_image_build_event(request, event)


def _execution_result(
    request: ImageBuildExecutionRequest,
    status: BuildStatus,
    events: list[ImageBuildStreamEventPlan],
    *,
    reason: str = "",
    staging_archive_object_id: str = "",
    staging_archive_object_key: str = "",
    staging_archive_size_bytes: int = 0,
    staging_archive_sha256: str = "",
) -> ImageBuildExecutionResult:
    complete = status is BuildStatus.Complete
    return ImageBuildExecutionResult(
        status=status,
        events=events,
        published_ref=(request.tag or request.image_id) if complete else "",
        artifact_path=request.manifest_path if complete else "",
        cache_metadata={
            "executor": "container-client",
            "container_id": request.session.container_id,
            "clip_version": str(request.session.clip_version),
            **(
                {
                    "staging_archive_object_id": staging_archive_object_id,
                    "staging_archive_object_key": staging_archive_object_key,
                    "staging_archive_size_bytes": str(staging_archive_size_bytes),
                    "staging_archive_sha256": staging_archive_sha256,
                }
                if staging_archive_object_id
                else {}
            ),
        }
        if complete
        else {},
        reason=reason.strip(),
    )


def _output_lines(*values: str) -> list[str]:
    return [line for value in values for line in value.splitlines() if line.strip()]
