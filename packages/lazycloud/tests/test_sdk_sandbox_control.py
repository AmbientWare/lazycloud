from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import pytest
from lazycloud.abstractions.sandbox import (
    Sandbox,
    SandboxConnectionError,
    SandboxInstance,
)
from shared.deployment_records import VolumeMount
from shared.http.errors import HttpApiError
from shared.http.pods import (
    CreatePodRequest,
    CreatePodResponse,
    PodFileSearchMatch,
    PodSandboxConnectResponse,
    PodSandboxCreateDirectoryRequest,
    PodSandboxCreateDirectoryResponse,
    PodSandboxCreateImageFromFilesystemRequest,
    PodSandboxCreateImageFromFilesystemResponse,
    PodSandboxDeleteDirectoryResponse,
    PodSandboxDeleteFileResponse,
    PodSandboxDownloadFileResponse,
    PodSandboxExecRequest,
    PodSandboxExecResponse,
    PodSandboxExposePortRequest,
    PodSandboxExposePortResponse,
    PodSandboxFileInfo,
    PodSandboxFindInFilesRequest,
    PodSandboxFindInFilesResponse,
    PodSandboxKillRequest,
    PodSandboxKillResponse,
    PodSandboxListFilesResponse,
    PodSandboxListProcessesResponse,
    PodSandboxListUrlsResponse,
    PodSandboxProcessInfo,
    PodSandboxReplaceInFilesRequest,
    PodSandboxReplaceInFilesResponse,
    PodSandboxSnapshotMemoryRequest,
    PodSandboxSnapshotMemoryResponse,
    PodSandboxStatFileResponse,
    PodSandboxStatusResponse,
    PodSandboxStderrResponse,
    PodSandboxStdoutResponse,
    PodSandboxUpdateNetworkPermissionsRequest,
    PodSandboxUpdateNetworkPermissionsResponse,
    PodSandboxUpdateTTLRequest,
    PodSandboxUpdateTTLResponse,
    PodSandboxUploadFileResponse,
    SandboxDashboardStatus,
    SandboxListRequest,
    SandboxListResponse,
    SandboxRow,
    SandboxStatsRequest,
    SandboxStatsResponse,
    SandboxTimeline,
    SandboxTimelineRequest,
)
from tests.fakes import FakeDeploymentClient, http_api_error

T = TypeVar("T")


@dataclass
class FakeSandboxPodClient:
    create_requests: list[CreatePodRequest] = field(default_factory=list)
    connect_requests: list[str] = field(default_factory=list)
    exec_requests: list[PodSandboxExecRequest] = field(default_factory=list)
    uploads: dict[str, bytes] = field(default_factory=dict)
    created_directories: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    deleted_directories: list[str] = field(default_factory=list)
    network_requests: list[PodSandboxUpdateNetworkPermissionsRequest] = field(default_factory=list)
    expose_requests: list[tuple[str, int]] = field(default_factory=list)
    ttl_requests: list[PodSandboxUpdateTTLRequest] = field(default_factory=list)
    list_requests: list[SandboxListRequest] = field(default_factory=list)
    stats_requests: list[SandboxStatsRequest] = field(default_factory=list)
    timeline_requests: list[SandboxTimelineRequest] = field(default_factory=list)
    killed: list[int] = field(default_factory=list)
    fail_create: bool = False
    fail_file: bool = False
    fail_terminate: bool = False
    fail_expose: bool = False
    fail_list_urls: bool = False
    connect_outcomes: list[PodSandboxConnectResponse | Exception] = field(default_factory=list)
    terminate_requests: list[str] = field(default_factory=list)
    terminated: list[str] = field(default_factory=list)
    next_pid: int = 100

    def create_pod(self, request: CreatePodRequest) -> CreatePodResponse:
        self.create_requests.append(request)
        stub_id = request.stub_id or ("checkpoint-stub" if request.checkpoint_id else "")
        if self.fail_create:
            return CreatePodResponse(stub_id=stub_id)
        return CreatePodResponse(
            container_id=f"ctr-{stub_id}",
            stub_id=stub_id,
        )

    def sandbox_connect(self, container_id: str) -> PodSandboxConnectResponse:
        self.connect_requests.append(container_id)
        if self.connect_outcomes:
            outcome = self.connect_outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return PodSandboxConnectResponse(stub_id="connected-stub")

    def sandbox_exec(
        self,
        container_id: str,
        request: PodSandboxExecRequest,
    ) -> PodSandboxExecResponse:
        self.exec_requests.append(request)
        self.next_pid += 1
        return PodSandboxExecResponse(pid=self.next_pid)

    def sandbox_status(self, container_id: str, pid: int) -> PodSandboxStatusResponse:
        return PodSandboxStatusResponse(status="complete", exit_code=0)

    def sandbox_stdout(self, container_id: str, pid: int) -> PodSandboxStdoutResponse:
        return PodSandboxStdoutResponse(stdout=f"stdout:{pid}\n")

    def sandbox_stderr(self, container_id: str, pid: int) -> PodSandboxStderrResponse:
        return PodSandboxStderrResponse(stderr="")

    def sandbox_kill(
        self,
        container_id: str,
        request: PodSandboxKillRequest,
    ) -> PodSandboxKillResponse:
        self.killed.append(request.pid)
        return PodSandboxKillResponse()

    def sandbox_list_processes(self, container_id: str) -> PodSandboxListProcessesResponse:
        return PodSandboxListProcessesResponse(
            processes=[PodSandboxProcessInfo(pid=101, command="python3 -c pass")],
        )

    def sandbox_upload_file(
        self,
        container_id: str,
        container_path: str,
        data: bytes,
        *,
        mode: int = 0o644,
    ) -> PodSandboxUploadFileResponse:
        if self.fail_file:
            raise HttpApiError("upload failed", status_code=500)
        self.uploads[container_path] = data
        return PodSandboxUploadFileResponse()

    def sandbox_download_file(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxDownloadFileResponse:
        return PodSandboxDownloadFileResponse.from_bytes(
            self.uploads.get(container_path, b"remote-data")
        )

    def sandbox_stat_file(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxStatFileResponse:
        return PodSandboxStatFileResponse(
            file_info=PodSandboxFileInfo(
                name=Path(container_path).name,
                size=len(self.uploads.get(container_path, b"")),
                is_dir=container_path.endswith("/"),
                mode=0o100644,
                permissions=0o644,
            ),
        )

    def sandbox_list_files(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxListFilesResponse:
        return PodSandboxListFilesResponse(
            files=[
                PodSandboxFileInfo(
                    name="app.py",
                    size=len(self.uploads.get("app.py", b"")),
                    mode=0o100644,
                    permissions=0o644,
                )
            ],
        )

    def sandbox_delete_file(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxDeleteFileResponse:
        self.deleted_files.append(container_path)
        self.uploads.pop(container_path, None)
        return PodSandboxDeleteFileResponse()

    def sandbox_create_directory(
        self,
        container_id: str,
        request: PodSandboxCreateDirectoryRequest,
    ) -> PodSandboxCreateDirectoryResponse:
        self.created_directories.append(request.container_path)
        return PodSandboxCreateDirectoryResponse()

    def sandbox_delete_directory(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxDeleteDirectoryResponse:
        self.deleted_directories.append(container_path)
        return PodSandboxDeleteDirectoryResponse()

    def sandbox_expose_port(
        self,
        container_id: str,
        request: PodSandboxExposePortRequest,
    ) -> PodSandboxExposePortResponse:
        if self.fail_expose:
            raise http_api_error("expose failed", status_code=503)
        self.expose_requests.append((container_id, request.port))
        return PodSandboxExposePortResponse(url=f"https://sandbox/{request.port}")

    def sandbox_update_network_permissions(
        self,
        container_id: str,
        request: PodSandboxUpdateNetworkPermissionsRequest,
    ) -> PodSandboxUpdateNetworkPermissionsResponse:
        self.network_requests.append(request)
        return PodSandboxUpdateNetworkPermissionsResponse(
            block_network=request.block_network,
            allow_list=request.allow_list,
        )

    def sandbox_network_permissions(
        self,
        container_id: str,
    ) -> PodSandboxUpdateNetworkPermissionsResponse:
        _ = container_id
        if not self.network_requests:
            return PodSandboxUpdateNetworkPermissionsResponse()
        request = self.network_requests[-1]
        return PodSandboxUpdateNetworkPermissionsResponse(
            block_network=request.block_network,
            allow_list=request.allow_list,
        )

    def sandbox_replace_in_files(
        self,
        container_id: str,
        request: PodSandboxReplaceInFilesRequest,
    ) -> PodSandboxReplaceInFilesResponse:
        return PodSandboxReplaceInFilesResponse()

    def sandbox_find_in_files(
        self,
        container_id: str,
        request: PodSandboxFindInFilesRequest,
    ) -> PodSandboxFindInFilesResponse:
        return PodSandboxFindInFilesResponse(
            results=[PodFileSearchMatch(path="app.py", text=request.pattern, line=1, column=7)],
        )

    def sandbox_update_ttl(
        self,
        container_id: str,
        request: PodSandboxUpdateTTLRequest,
    ) -> PodSandboxUpdateTTLResponse:
        self.ttl_requests.append(request)
        return PodSandboxUpdateTTLResponse(ttl=request.ttl)

    def sandbox_terminate(self, container_id: str) -> None:
        self.terminate_requests.append(container_id)
        if self.fail_terminate:
            raise http_api_error("stop failed", status_code=500)
        self.terminated.append(container_id)

    def sandbox_create_image_from_filesystem(
        self,
        container_id: str,
        request: PodSandboxCreateImageFromFilesystemRequest,
    ) -> PodSandboxCreateImageFromFilesystemResponse:
        return PodSandboxCreateImageFromFilesystemResponse(image_id="image-1")

    def sandbox_snapshot_memory(
        self,
        container_id: str,
        request: PodSandboxSnapshotMemoryRequest,
    ) -> PodSandboxSnapshotMemoryResponse:
        return PodSandboxSnapshotMemoryResponse(checkpoint_id="checkpoint-1")

    def sandbox_list_urls(self, container_id: str) -> PodSandboxListUrlsResponse:
        if self.fail_list_urls:
            raise http_api_error("list urls failed", status_code=503)
        return PodSandboxListUrlsResponse(urls={8000: "https://sandbox/8000"})

    def sandbox_list(self, request: SandboxListRequest | None = None) -> SandboxListResponse:
        selected = request or SandboxListRequest()
        self.list_requests.append(selected)
        return SandboxListResponse(
            data=(
                SandboxRow(
                    id="sandbox-1",
                    stub_id="stub-sandbox",
                    name="sandbox",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    status=SandboxDashboardStatus.Running,
                    container_id="ctr-stub-sandbox",
                ),
            )
        )

    def sandbox_stats(self, request: SandboxStatsRequest | None = None) -> SandboxStatsResponse:
        selected = request or SandboxStatsRequest()
        self.stats_requests.append(selected)
        return SandboxStatsResponse(
            concurrent=1,
            total_created=1,
            rate_per_second=0.0,
            status_counts={
                SandboxDashboardStatus.Pending: 0,
                SandboxDashboardStatus.Running: 1,
                SandboxDashboardStatus.Stopping: 0,
                SandboxDashboardStatus.Stopped: 0,
                SandboxDashboardStatus.Failed: 0,
            },
        )

    def sandbox_timeline(self, request: SandboxTimelineRequest) -> SandboxTimeline:
        self.timeline_requests.append(request)
        return SandboxTimeline(
            container_id=request.container_id or "ctr-stub-sandbox",
            status=SandboxDashboardStatus.Running,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


@dataclass(frozen=True)
class ExportableVolume:
    name: str
    mount_path: str

    def export(self) -> VolumeMount:
        return VolumeMount(name=self.name, mount_path=self.mount_path)


def _bind_internal_state(resource: T, /, **values: object) -> T:
    for name, value in values.items():
        setattr(resource, name, value)
    return resource


def test_sandbox_memory_restore_does_not_prepare_a_new_stub() -> None:
    pod = FakeSandboxPodClient()
    sandbox = _bind_internal_state(
        Sandbox(_app_slug="test"),
        client=pod,
    )

    restored = sandbox.create_from_memory_snapshot("checkpoint-warm")

    assert restored.stub_id == "connected-stub"
    assert pod.create_requests == [CreatePodRequest(checkpoint_id="checkpoint-warm")]
    assert pod.connect_requests == ["ctr-checkpoint-stub"]


def test_sandbox_create_retries_only_typed_pending_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def skip_delay(seconds: float) -> None:
        del seconds

    monkeypatch.setattr("lazycloud.abstractions.sandbox.time.sleep", skip_delay)
    pod = FakeSandboxPodClient(
        connect_outcomes=[
            http_api_error("container ctr-stub-1 is pending", status_code=503),
            http_api_error("worker address not published", status_code=503),
            PodSandboxConnectResponse(stub_id="ready-stub"),
        ]
    )
    sandbox = _bind_internal_state(
        Sandbox(_app_slug="test"),
        stub_id="stub-1",
        client=pod,
    )

    instance = sandbox.create()

    assert instance.container_id == "ctr-stub-1"
    assert instance.stub_id == "ready-stub"
    assert len(pod.create_requests) == 1
    assert pod.connect_requests == ["ctr-stub-1", "ctr-stub-1", "ctr-stub-1"]
    assert pod.exec_requests == []
    assert pod.terminated == []


def test_sandbox_create_does_not_retry_non_pending_http_failures() -> None:
    cause = http_api_error("terminal sandbox state", status_code=401)
    pod = FakeSandboxPodClient(connect_outcomes=[cause])
    sandbox = _bind_internal_state(
        Sandbox(_app_slug="test"),
        stub_id="stub-1",
        client=pod,
    )

    with pytest.raises(SandboxConnectionError) as captured:
        sandbox.create()

    assert captured.value.container_id == "ctr-stub-1"
    assert captured.value.state == "terminal sandbox state"
    assert captured.value.__cause__ is cause
    assert pod.connect_requests == ["ctr-stub-1"]
    assert pod.terminate_requests == ["ctr-stub-1"]
    assert pod.terminated == ["ctr-stub-1"]


def test_sandbox_create_timeout_preserves_state_and_cleans_up_once() -> None:
    cause = http_api_error("container ctr-stub-1 is pending", status_code=503)
    pod = FakeSandboxPodClient(connect_outcomes=[cause])
    sandbox = _bind_internal_state(
        Sandbox(_app_slug="test"),
        stub_id="stub-1",
        client=pod,
    )

    with pytest.raises(SandboxConnectionError, match="did not become ready") as captured:
        sandbox.create(timeout_seconds=0)

    assert captured.value.container_id == "ctr-stub-1"
    assert captured.value.state == "container ctr-stub-1 is pending"
    assert captured.value.__cause__ is cause
    assert pod.connect_requests == ["ctr-stub-1"]
    assert pod.terminate_requests == ["ctr-stub-1"]
    assert pod.terminated == ["ctr-stub-1"]


def test_sandbox_create_transport_and_cleanup_failures_are_observable() -> None:
    transport_error = TimeoutError("connect timed out")
    pod = FakeSandboxPodClient(
        connect_outcomes=[transport_error],
        fail_terminate=True,
    )
    sandbox = _bind_internal_state(
        Sandbox(_app_slug="test"),
        stub_id="stub-1",
        client=pod,
    )

    with pytest.raises(SandboxConnectionError, match="failed to terminate") as captured:
        sandbox.create()

    assert captured.value.container_id == "ctr-stub-1"
    assert captured.value.state == "TimeoutError: connect timed out"
    assert isinstance(captured.value.cleanup_error, HttpApiError)
    readiness_error = captured.value.__cause__
    assert isinstance(readiness_error, SandboxConnectionError)
    assert readiness_error.__cause__ is transport_error
    assert pod.connect_requests == ["ctr-stub-1"]
    assert pod.terminate_requests == ["ctr-stub-1"]
    assert pod.terminated == []


@pytest.mark.parametrize("port", [0, 65536, True])
def test_sandbox_constructor_rejects_invalid_declared_ports(port: int) -> None:
    with pytest.raises(ValueError, match="integers between 1 and 65535"):
        Sandbox(_app_slug="test", ports=[port])


def test_sandbox_url_operations_wrap_transport_failures_for_sync_and_async() -> None:
    pod = FakeSandboxPodClient(fail_expose=True, fail_list_urls=True)
    instance = SandboxInstance(container_id="ctr-1", stub_id="stub-1", client=pod)

    with pytest.raises(SandboxConnectionError, match="expose failed") as sync_expose:
        instance.expose_port(8000)
    with pytest.raises(SandboxConnectionError, match="list urls failed") as sync_list:
        instance.list_urls()
    assert isinstance(sync_expose.value.__cause__, HttpApiError)
    assert isinstance(sync_list.value.__cause__, HttpApiError)

    async def scenario() -> None:
        with pytest.raises(SandboxConnectionError, match="expose failed") as async_expose:
            await instance.aio.expose_port(8000)
        with pytest.raises(SandboxConnectionError, match="list urls failed") as async_list:
            await instance.aio.list_urls()
        assert isinstance(async_expose.value.__cause__, HttpApiError)
        assert isinstance(async_list.value.__cause__, HttpApiError)

    asyncio.run(scenario())


def test_sandbox_prepare_emits_canonical_deployment_request() -> None:
    deployment = FakeDeploymentClient(stub_id="stub-sandbox")
    sandbox = Sandbox(
        _app_slug="test",
        keep_warm_seconds=900,
        sync_local_dir=True,
        docker_enabled=True,
        preemptible=True,
        block_network=True,
        ports=[8000, 9000],
        pool="gpu-pool",
        authorized=True,
        volumes=[ExportableVolume(name="data", mount_path="/data")],
    )
    _bind_internal_state(sandbox, deployment_client=deployment)

    assert sandbox.prepare(workspace="platform") == "stub-sandbox"

    request = deployment.requests[0]
    assert request.stub_type == "sandbox"
    assert request.workspace == "platform"
    assert (
        request.authorized,
        request.block_network,
        request.docker_enabled,
        request.preemptible,
    ) == (True, True, True, True)
    assert request.pool == "gpu-pool"
    assert [(volume.id, volume.mount_path) for volume in request.volumes] == [("data", "/data")]
