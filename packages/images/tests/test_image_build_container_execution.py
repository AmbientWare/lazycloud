from __future__ import annotations

import json
import threading
from base64 import b64decode
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from api.server.services import ApiServices
from foundation.io_utils import OutputMessage
from images.building import (
    ImageBuildStreamEventPlan,
    build_image_plan,
    marshal_registry_credentials,
    plan_image_build_registry_credentials,
    plan_image_build_session,
    registry_credentials_for_image,
)
from images.execution import ImageBuildExecutionRequest
from images.scheduler_lifecycle import SchedulerImageBuildContainerStateStore
from images.scheduling import plan_image_build_container_request
from images.service import ImageBuildService
from scheduler.containers import (
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
)
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    SchedulerContainerAddress,
    SchedulerWorkerRequest,
)
from shared.contracts import ContractModel
from shared.image_building.authoring import ImageBuildStep, ImageBuildStepKind, ImageSpec
from shared.image_building.records import BuildStatus
from shared.scheduling import SchedulerContainerState
from tests.real_redis import RealRedisActors
from worker.container_client.control import ContainerServiceTransport
from worker.container_client.models import (
    ContainerClientConnectionOptions,
    ContainerExecResponse,
    ContainerKillResponse,
    ContainerServiceMethod,
    ContainerStatusResponse,
)
from worker_repository.image_build_container_execution import (
    PRIVATE_INPUTS_REQUIRE_V2_REASON,
    ContainerServiceImageBuildExecutor,
    ImageBuildContainerExecutorFactoryResult,
    ImageBuildContainerExecutorFactoryStatus,
)
from worker_repository.image_build_credentials import (
    ImageBuildCredentialLease,
)
from worker_repository.image_build_scheduler_execution import SchedulerImageBuildExecutor


def test_container_service_image_build_executor_preserves_v2_worker_failure(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    failure_reason = "registry inspection failed: invalid authorization credential"
    client = _FakeBuildContainerClient(
        statuses=[
            ContainerStatusResponse(
                ok=False,
                status="failed",
                exit_code=1,
                error_msg=failure_reason,
            )
        ]
    )

    result = ContainerServiceImageBuildExecutor(client).execute(request)

    assert result.status is BuildStatus.Failed
    assert result.reason == failure_reason
    assert any(event.error.strip() == f"Build failed: {failure_reason}" for event in result.events)
    assert client.kill_calls == [request.session.container_id]


def test_container_service_image_build_executor_owns_terminal_log_shutdown(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    client = _KillTerminatedLogClient(
        statuses=[ContainerStatusResponse(status="complete", exit_code=0)]
    )

    result = ContainerServiceImageBuildExecutor(
        client,
        log_stream_drain_wait_seconds=0.5,
    ).execute(request)

    assert result.status is BuildStatus.Complete
    assert client.kill_calls == [request.session.container_id]
    assert client.log_stream_stopped.is_set()
    assert not any("did not stop" in event.message for event in result.events)


def test_container_service_image_build_executor_reports_unjoined_log_stream(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    client = _BlockedLogClient(statuses=[ContainerStatusResponse(status="complete", exit_code=0)])
    try:
        result = ContainerServiceImageBuildExecutor(
            client,
            log_stream_drain_wait_seconds=0,
        ).execute(request)

        assert result.status is BuildStatus.Complete
        assert client.kill_calls == [request.session.container_id]
        assert any(
            "did not stop before the shutdown deadline" in event.message for event in result.events
        )
    finally:
        client.release_log_stream.set()
        assert client.log_stream_stopped.wait(timeout=1)


def test_container_service_image_build_executor_does_not_fail_on_log_stream_error(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    client = _FakeBuildContainerClient(
        statuses=[ContainerStatusResponse(status="complete", exit_code=0)],
        stream_log_error=RuntimeError("log stream unavailable"),
    )

    result = ContainerServiceImageBuildExecutor(
        client,
        log_stream_drain_wait_seconds=0.1,
    ).execute(request)

    assert result.status is BuildStatus.Complete
    assert any("log stream unavailable" in event.message for event in result.events)


def test_container_service_image_build_executor_emits_live_events(
    tmp_path: Path,
) -> None:
    emitted: list[ImageBuildStreamEventPlan] = []
    request = _request(
        tmp_path,
        ImageSpec(
            ignore_python=True,
            build_steps=[ImageBuildStep(kind=ImageBuildStepKind.Shell, command="echo ok")],
        ),
        clip_version=1,
    ).model_copy(update={"event_sink": emitted.append})
    client = _FakeBuildContainerClient(
        statuses=[ContainerStatusResponse(status="running")],
        exec_responses=[ContainerExecResponse(stdout="ok\n")],
        archive_messages=[OutputMessage(msg="archive progress: 100%\n", archiving=True)],
    )

    result = ContainerServiceImageBuildExecutor(client).execute(request)

    assert result.status is BuildStatus.Complete
    assert any("running: echo ok" in event.message for event in emitted)
    assert any(event.message.strip() == "ok" for event in emitted)
    assert any("archive progress: 100%" in event.message for event in emitted)


def test_container_service_image_build_executor_rejects_private_inputs_on_v1(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        ImageSpec(
            ignore_python=True,
            build_steps=[ImageBuildStep(kind=ImageBuildStepKind.Shell, command="echo ok")],
        ),
        clip_version=1,
    ).model_copy(
        update={
            "registry_credential_payload": json.dumps(
                {
                    "registry": "registry.example.com",
                    "type": "basic",
                    "credentials": {"password": "registry-secret"},
                },
                sort_keys=True,
            )
        }
    )
    client = _FakeBuildContainerClient(
        statuses=[ContainerStatusResponse(status="running")],
        exec_responses=[ContainerExecResponse(stdout="ok\n")],
        archive_messages=[OutputMessage(msg="archive complete\n", archiving=True)],
    )

    result = ContainerServiceImageBuildExecutor(client).execute(request)

    assert result.status is BuildStatus.Failed
    assert result.reason == PRIVATE_INPUTS_REQUIRE_V2_REASON
    assert client.exec_calls == []
    assert client.archive_calls == []
    assert client.kill_calls == [request.session.container_id]
    assert "registry-secret" not in result.model_dump_json()


def test_scheduler_image_build_executor_submits_waits_and_delegates(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    scheduler = _ContainerRequestScheduler()
    inner_executor = ContainerServiceImageBuildExecutor(
        _FakeBuildContainerClient(
            statuses=[ContainerStatusResponse(status="complete", exit_code=0)]
        )
    )
    factory = _SequencedExecutorFactory(
        [
            ImageBuildContainerExecutorFactoryResult(
                status=ImageBuildContainerExecutorFactoryStatus.MissingAddress,
                container_id=request.session.container_id,
                reason="worker address pending",
            ),
            ImageBuildContainerExecutorFactoryResult(
                status=ImageBuildContainerExecutorFactoryStatus.Ready,
                container_id=request.session.container_id,
                service_url="worker.example.com:443",
                executor=inner_executor,
            ),
        ]
    )
    pending_state = _PendingContainerState()
    executor = SchedulerImageBuildExecutor(
        scheduler,
        factory,
        pending_state,
        pool_selector="image-build",
        address_wait_timeout_seconds=1,
        address_poll_interval_seconds=0,
        sleep=lambda _seconds: None,
    )

    result = executor.execute(request)

    assert result.status is BuildStatus.Complete
    assert scheduler.calls[0].workspace_id == request.workspace_id
    assert scheduler.calls[0].pool_selector == "image-build"
    assert scheduler.calls[0].requested_placement is None
    assert factory.calls == [request.session.container_id, request.session.container_id]
    assert result.cache_metadata["scheduler_submit_status"] == "queued"
    assert result.cache_metadata["container_service_url"] == "worker.example.com:443"
    assert result.cache_metadata["executor"] == "container-client"
    assert pending_state.deleted == []


def test_scheduler_image_build_executor_reports_scheduler_error(tmp_path: Path) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    scheduler = _ContainerRequestScheduler(
        SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Error,
            container_id=request.session.container_id,
            reason="scheduler unavailable",
        )
    )
    factory = _SequencedExecutorFactory([])
    pending_state = _PendingContainerState()
    executor = SchedulerImageBuildExecutor(
        scheduler,
        factory,
        pending_state,
    )

    result = executor.execute(request)

    assert result.status is BuildStatus.Failed
    assert result.reason == "scheduler unavailable"
    assert factory.calls == []
    assert pending_state.deleted == [request.session.container_id]


def test_scheduler_image_build_executor_does_not_expose_submission_exception(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    secret = "scheduler-internal-sensitive-detail"
    pending_state = _PendingContainerState()
    executor = SchedulerImageBuildExecutor(
        _FailingContainerRequestScheduler(secret),
        _SequencedExecutorFactory([]),
        pending_state,
    )

    result = executor.execute(request)

    assert result.status is BuildStatus.Failed
    assert result.reason == "image build container request submission failed (RuntimeError)"
    assert secret not in result.model_dump_json()
    assert secret not in "".join(event.model_dump_json() for event in result.events)
    assert pending_state.deleted == [request.session.container_id]


def test_scheduler_image_build_executor_cancels_pending_request_after_address_timeout(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    pending_state = _PendingContainerState()
    executor = SchedulerImageBuildExecutor(
        _ContainerRequestScheduler(),
        _SequencedExecutorFactory([]),
        pending_state,
        address_wait_timeout_seconds=0,
    )

    result = executor.execute(request)

    assert result.status is BuildStatus.Failed
    assert result.reason == "worker address missing"
    assert pending_state.deleted == [request.session.container_id]
    assert result.cache_metadata["scheduler_cancel_status"] == "complete"


def test_scheduler_image_build_executor_tombstone_drops_queued_request_after_timeout(
    tmp_path: Path,
    real_redis_actors: RealRedisActors,
) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    plan = plan_image_build_container_request(request, workspace_id=request.workspace_id)
    containers.set_container_state(
        SchedulerContainerState(
            container_id=request.session.container_id,
            stub_id="image-build",
            workspace_id=request.workspace_id,
            image_build_id=request.build_id,
            image_id=request.image_id,
        )
    )
    workers.enqueue_container_request(plan.scheduler_request)
    executor = SchedulerImageBuildExecutor(
        _ContainerRequestScheduler(),
        _SequencedExecutorFactory([]),
        SchedulerImageBuildContainerStateStore(containers),
        address_wait_timeout_seconds=0,
    )

    result = executor.execute(request)

    assert result.status is BuildStatus.Failed
    assert containers.get_container_state(request.session.container_id) is None
    assert containers.is_container_cancelled(request.session.container_id)
    assert workers.claim_ready_container_requests(limit=1) == []
    assert workers.list_workers() == []
    assert workers.get_next_container_request("unassigned-worker") is None
    assert redis.sorted_set_cardinality(workers.keys.container_requests()) == 0
    assert redis.sorted_set_cardinality(workers.keys.container_request_claims()) == 0
    assert redis.hash_length(workers.keys.container_request_payloads()) == 0
    assert redis.hash_length(workers.keys.container_request_claim_owners()) == 0


def test_scheduler_image_build_executor_cancels_pending_request_when_credentials_cannot_stage(
    tmp_path: Path,
) -> None:
    credentials = {"username": "builder", "password": "private-secret"}
    source_image = "registry.example.com/team/base:latest"
    request = _request(
        tmp_path,
        ImageSpec(base=source_image, ignore_python=True),
        clip_version=2,
    ).model_copy(
        update={
            "credential_plan": plan_image_build_registry_credentials(
                source_image=source_image,
                credentials=credentials,
            ),
            "registry_credential_payload": marshal_registry_credentials(
                registry_credentials_for_image(source_image, credentials)
            ),
        }
    )
    pending_state = _PendingContainerState()
    executor = SchedulerImageBuildExecutor(
        _ContainerRequestScheduler(),
        _SequencedExecutorFactory([]),
        pending_state,
    )

    result = executor.execute(request)

    assert result.status is BuildStatus.Failed
    assert result.reason == "image build source credentials require a credential cache"
    assert pending_state.deleted == [request.session.container_id]
    assert result.cache_metadata["scheduler_cancel_status"] == "complete"


def test_scheduler_image_build_executor_defers_placement_to_the_workspace_policy(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, ImageSpec(packages=["httpx"]), clip_version=2)
    scheduler = _ContainerRequestScheduler()
    executor = SchedulerImageBuildExecutor(
        scheduler,
        _SequencedExecutorFactory(
            [
                ImageBuildContainerExecutorFactoryResult(
                    status=ImageBuildContainerExecutorFactoryStatus.Ready,
                    container_id=request.session.container_id,
                    service_url="worker.example.com:443",
                    executor=ContainerServiceImageBuildExecutor(
                        _FakeBuildContainerClient(
                            statuses=[ContainerStatusResponse(status="complete", exit_code=0)]
                        )
                    ),
                )
            ]
        ),
        _PendingContainerState(),
    )

    result = executor.execute(request)

    assert result.status is BuildStatus.Complete
    assert scheduler.calls[0].pool_selector == ""
    # Without a dedicated build pool the workspace policy chooses where the build
    # runs; forcing managed capacity strands builds on connected-provider
    # deployments that have no local worker.
    assert scheduler.calls[0].requested_placement is None


def test_scheduler_stages_and_cleans_private_build_credentials(tmp_path: Path) -> None:
    credentials = {"username": "builder", "password": "private-secret"}
    payload = marshal_registry_credentials(
        registry_credentials_for_image("registry.example.com/team/base:latest", credentials)
    )
    request = _request(
        tmp_path,
        ImageSpec(base="registry.example.com/team/base:latest", ignore_python=True),
        clip_version=2,
    ).model_copy(
        update={
            "credential_plan": plan_image_build_registry_credentials(
                source_image="registry.example.com/team/base:latest",
                credentials=credentials,
            ),
            "registry_credential_payload": payload,
            "build_args": {"PRIVATE_ARG": "private-build-argument"},
        }
    )
    scheduler = _ContainerRequestScheduler()
    inner_executor = ContainerServiceImageBuildExecutor(
        _FakeBuildContainerClient(
            statuses=[ContainerStatusResponse(status="complete", exit_code=0)]
        )
    )
    factory = _SequencedExecutorFactory(
        [
            ImageBuildContainerExecutorFactoryResult(
                status=ImageBuildContainerExecutorFactoryStatus.Ready,
                container_id=request.session.container_id,
                service_url="worker.example.com:443",
                executor=inner_executor,
            )
        ]
    )
    credential_cache = _RecordingCredentialCache()
    executor = SchedulerImageBuildExecutor(
        scheduler,
        factory,
        _PendingContainerState(),
        credential_cache=credential_cache,
    )

    result = executor.execute(request)

    assert result.status is BuildStatus.Complete
    assert len(credential_cache.puts) == 1
    cache_key, lease = credential_cache.puts[0]
    assert cache_key
    assert lease.workspace_id == request.workspace_id
    assert lease.build_id == request.build_id
    assert lease.container_id == request.session.container_id
    assert lease.private_inputs.registry_auth is not None
    assert lease.private_inputs.registry_auth.registry == "registry.example.com"
    assert b64decode(lease.private_inputs.registry_auth.auth).decode("utf-8") == (
        "builder:private-secret"
    )
    assert lease.private_inputs.build_args == {"PRIVATE_ARG": "private-build-argument"}
    assert credential_cache.deletes == [cache_key]
    assert "private-secret" not in json.dumps(scheduler.calls[0].payload)
    assert "private-build-argument" not in json.dumps(scheduler.calls[0].payload)


def test_scheduler_private_input_validation_never_emits_secret_values(tmp_path: Path) -> None:
    secret = "malformed-registry-secret-value"
    request = _request(
        tmp_path,
        ImageSpec(base="registry.example.com/team/base:latest", ignore_python=True),
        clip_version=2,
    ).model_copy(
        update={
            "credential_plan": plan_image_build_registry_credentials(
                source_image="registry.example.com/team/base:latest",
                credentials={"username": "builder", "password": secret},
            ),
            "registry_credential_payload": (
                f'{{"registry":"registry.example.com","credentials":{{"password":"{secret}'
            ),
        }
    )
    scheduler = _ContainerRequestScheduler()
    pending_state = _PendingContainerState()
    executor = SchedulerImageBuildExecutor(
        scheduler,
        _SequencedExecutorFactory([]),
        pending_state,
        credential_cache=_RecordingCredentialCache(),
    )

    result = executor.execute(request)

    assert result.status is BuildStatus.Failed
    assert result.reason == "image build private input validation failed (ValueError)"
    assert scheduler.calls == []
    assert pending_state.deleted == [request.session.container_id]
    assert secret not in repr(request)
    assert secret not in repr(result)
    assert secret not in result.model_dump_json()
    assert secret not in "".join(event.model_dump_json() for event in result.events)


def test_scheduler_failure_reaches_real_image_service_and_cleans_pending_state_and_credentials(
    isolated_services: ApiServices,
) -> None:
    credentials = {"username": "builder", "password": "private-secret"}
    source_image = "registry.example.com/team/base:latest"
    credential_cache = _RecordingCredentialCache()
    pending_state = _PendingContainerState()
    scheduling_failure = "no worker capacity available"
    executor = SchedulerImageBuildExecutor(
        _ContainerRequestScheduler(),
        _SequencedExecutorFactory(
            [
                ImageBuildContainerExecutorFactoryResult(
                    status=ImageBuildContainerExecutorFactoryStatus.SchedulingFailed,
                    container_id="build-container",
                    reason=scheduling_failure,
                )
            ]
        ),
        pending_state,
        credential_cache=credential_cache,
    )
    service = ImageBuildService(
        isolated_services.images.context,
        events=isolated_services.events,
        executor=executor,
    )

    execution = service.execute(
        ImageSpec(base=source_image, ignore_python=True),
        credential_plan=plan_image_build_registry_credentials(
            source_image=source_image,
            credentials=credentials,
        ),
        registry_credential_payload=marshal_registry_credentials(
            registry_credentials_for_image(source_image, credentials)
        ),
        build_args={"PRIVATE_ARG": "private-build-argument"},
    )

    assert execution.record.status is BuildStatus.Failed
    assert execution.record.error == scheduling_failure
    assert pending_state.deleted == [execution.session.container_id]
    assert len(credential_cache.puts) == 1
    assert credential_cache.deletes == [credential_cache.puts[0][0]]
    assert service.get(execution.record.id).error == scheduling_failure


def _request(
    tmp_path: Path,
    image: ImageSpec,
    *,
    clip_version: int,
    build_args: dict[str, str] | None = None,
) -> ImageBuildExecutionRequest:
    plan = build_image_plan(image)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    dockerfile_path = build_dir / "Dockerfile"
    manifest_path = build_dir / "manifest.json"
    dockerfile_path.write_text(plan.dockerfile, encoding="utf-8")
    manifest_path.write_text("{}", encoding="utf-8")
    session = plan_image_build_session(
        image,
        clip_version=clip_version,
        image_id=plan.image_id,
        build_id="build-1",
        container_id="build-container-1",
    )
    return ImageBuildExecutionRequest(
        build_id="build-1",
        workspace_id="workspace-1",
        image_id=plan.image_id,
        tag="registry.example.com/team/app:latest",
        build_dir=str(build_dir),
        dockerfile_path=str(dockerfile_path),
        manifest_path=str(manifest_path),
        plan=plan,
        session=session,
        build_args=build_args or {},
    )


class _FakeBuildContainerClient:
    def __init__(
        self,
        *,
        statuses: list[ContainerStatusResponse],
        exec_responses: list[ContainerExecResponse] | None = None,
        archive_messages: list[OutputMessage] | None = None,
        log_messages: list[OutputMessage] | None = None,
        stream_log_error: Exception | None = None,
    ) -> None:
        self.statuses = list(statuses)
        self.exec_responses = list(exec_responses or [])
        self.archive_messages = list(archive_messages or [])
        self.log_messages = list(log_messages or [])
        self.stream_log_error = stream_log_error
        self.exec_calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.archive_calls: list[tuple[str, str]] = []
        self.kill_calls: list[str] = []
        self.stream_log_calls: list[str] = []

    def status(self, container_id: str) -> ContainerStatusResponse:
        del container_id
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def exec(
        self,
        container_id: str,
        command: str,
        env: Sequence[str] = (),
    ) -> ContainerExecResponse:
        self.exec_calls.append((container_id, command, tuple(env)))
        if self.exec_responses:
            return self.exec_responses.pop(0)
        return ContainerExecResponse()

    def archive(
        self,
        container_id: str,
        image_id: str,
        output: Callable[[OutputMessage], None],
    ) -> None:
        self.archive_calls.append((container_id, image_id))
        for message in self.archive_messages:
            output(message)

    def stream_logs(
        self,
        container_id: str,
        output: Callable[[OutputMessage], None],
    ) -> None:
        self.stream_log_calls.append(container_id)
        if self.stream_log_error is not None:
            raise self.stream_log_error
        for message in self.log_messages:
            output(message)

    def kill(self, container_id: str) -> ContainerKillResponse:
        self.kill_calls.append(container_id)
        return ContainerKillResponse()


class _KillTerminatedLogClient(_FakeBuildContainerClient):
    def __init__(self, *, statuses: list[ContainerStatusResponse]) -> None:
        super().__init__(statuses=statuses)
        self.terminal = threading.Event()
        self.log_stream_stopped = threading.Event()

    def stream_logs(
        self,
        container_id: str,
        output: Callable[[OutputMessage], None],
    ) -> None:
        del output
        self.stream_log_calls.append(container_id)
        self.terminal.wait()
        self.log_stream_stopped.set()

    def kill(self, container_id: str) -> ContainerKillResponse:
        response = super().kill(container_id)
        self.terminal.set()
        return response


class _BlockedLogClient(_FakeBuildContainerClient):
    def __init__(self, *, statuses: list[ContainerStatusResponse]) -> None:
        super().__init__(statuses=statuses)
        self.release_log_stream = threading.Event()
        self.log_stream_stopped = threading.Event()

    def stream_logs(
        self,
        container_id: str,
        output: Callable[[OutputMessage], None],
    ) -> None:
        del output
        self.stream_log_calls.append(container_id)
        self.release_log_stream.wait()
        self.log_stream_stopped.set()


class _AddressResolver:
    def __init__(self, addresses: dict[str, str]) -> None:
        self.addresses = addresses

    def worker_address(self, container_id: str) -> SchedulerContainerAddress | None:
        address = self.addresses.get(container_id)
        if address is None:
            return None
        return SchedulerContainerAddress(container_id=container_id, address=address)

    def scheduling_failure(self, container_id: str) -> None:
        del container_id
        return None


class _TransportFactory:
    def __init__(self, transport: ContainerServiceTransport) -> None:
        self.transport = transport
        self.options: list[ContainerClientConnectionOptions] = []

    def create_transport(
        self,
        options: ContainerClientConnectionOptions,
    ) -> ContainerServiceTransport:
        self.options.append(options)
        return self.transport


class _FactoryTransport:
    def __init__(self) -> None:
        self.unary_calls: list[tuple[ContainerServiceMethod, ContractModel, float | None]] = []

    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, bool | str | int]:
        self.unary_calls.append((method, request, timeout_seconds))
        if method is ContainerServiceMethod.ContainerStatus:
            return {"ok": True, "status": "complete", "exit_code": 0}
        return {"ok": True}

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterable[ContractModel]:
        del method, request, timeout_seconds
        return ()


class _ContainerRequestScheduler:
    def __init__(self, result: SchedulerContainerSubmitResult | None = None) -> None:
        self.result = result
        self.calls: list[SchedulerWorkerRequest] = []

    def submit(self, request: SchedulerWorkerRequest) -> SchedulerContainerSubmitResult:
        self.calls.append(request)
        return self.result or SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
            reason="container request queued for scheduling",
        )


class _FailingContainerRequestScheduler:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def submit(self, request: SchedulerWorkerRequest) -> SchedulerContainerSubmitResult:
        del request
        raise RuntimeError(self.reason)


class _SequencedExecutorFactory:
    def __init__(self, results: list[ImageBuildContainerExecutorFactoryResult]) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def create(self, container_id: str) -> ImageBuildContainerExecutorFactoryResult:
        self.calls.append(container_id)
        if self.results:
            return self.results.pop(0)
        return ImageBuildContainerExecutorFactoryResult(
            status=ImageBuildContainerExecutorFactoryStatus.MissingAddress,
            container_id=container_id,
            reason="worker address missing",
        )


class _PendingContainerState:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.marked_stopping: list[tuple[str, int]] = []

    def delete_pending_build_container(self, container_id: str) -> bool:
        self.deleted.append(container_id)
        return True

    def mark_build_container_stopping(self, container_id: str, ttl_seconds: int) -> bool:
        self.marked_stopping.append((container_id, ttl_seconds))
        return True


class _RecordingCredentialCache:
    def __init__(self) -> None:
        self.puts: list[tuple[str, ImageBuildCredentialLease]] = []
        self.deletes: list[str] = []

    def put(self, cache_key: str, lease: ImageBuildCredentialLease) -> None:
        self.puts.append((cache_key, lease))

    def delete(self, cache_key: str) -> None:
        self.deletes.append(cache_key)
