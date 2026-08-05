from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

import pytest
from worker.container_client.models import (
    ContainerArchiveResponse,
    ContainerExecRequest,
    ContainerExecResponse,
    ContainerKillRequest,
    ContainerSandboxCreateDirectoryRequest,
    ContainerSandboxDeleteDirectoryRequest,
    ContainerSandboxDeleteFileRequest,
    ContainerSandboxDownloadFileRequest,
    ContainerSandboxExecRequest,
    ContainerSandboxExposePortRequest,
    ContainerSandboxFindInFilesRequest,
    ContainerSandboxKillRequest,
    ContainerSandboxListExposedPortsRequest,
    ContainerSandboxListFilesRequest,
    ContainerSandboxListProcessesRequest,
    ContainerSandboxReplaceInFilesRequest,
    ContainerSandboxStatFileRequest,
    ContainerSandboxStatusRequest,
    ContainerSandboxStderrRequest,
    ContainerSandboxStdoutRequest,
    ContainerSandboxUnexposePortRequest,
    ContainerSandboxUpdateNetworkPermissionsRequest,
    ContainerSandboxUploadFileRequest,
    ContainerStatusRequest,
    ContainerStreamLogsRequest,
    ContainerWorkspaceSyncOperation,
    SyncContainerWorkspaceRequest,
)
from worker.container_service.models import (
    SandboxProcessEvent,
    SandboxProcessEventType,
    WorkerContainerServiceInstance,
    WorkerSandboxProcess,
)
from worker.container_service.service import WorkerContainerService
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.container_service.supervisor_process_manager import (
    SupervisorRequest,
    _SupervisorConnectionCoordinator,
    _SupervisorTransport,
)
from worker.runtime_config import OciRuntimeName
from worker.sandbox_server import (
    SandboxLogStream,
    SandboxProcessLogEntry,
)


@dataclass(slots=True)
class ProcessManager:
    events: list[SandboxProcessEvent] = field(default_factory=list)
    statuses: dict[int, int | None] = field(default_factory=dict)
    stdout_by_pid: dict[int, str] = field(default_factory=dict)
    stderr_by_pid: dict[int, str] = field(default_factory=dict)
    killed: list[int] = field(default_factory=list)
    ready_value: bool = True
    ready_error: RuntimeError | None = None
    ready_calls: int = 0
    acknowledgements: list[tuple[int, int, bool]] = field(default_factory=list)
    cleanup_count: int = 0
    stream_calls: list[tuple[list[str], str, list[str]]] = field(default_factory=list)
    processes: list[WorkerSandboxProcess] = field(default_factory=list)
    cleanup_event: Event = field(default_factory=Event)

    def ready(self) -> bool:
        self.ready_calls += 1
        if self.ready_error is not None:
            raise self.ready_error
        return self.ready_value

    def ack(self, pid: int, seq: int, *, ok: bool) -> None:
        self.acknowledgements.append((pid, seq, ok))

    def stream_exec(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: list[str],
    ) -> list[SandboxProcessEvent]:
        self.stream_calls.append((argv, cwd, env))
        return self.events

    def status(self, pid: int) -> int | None:
        return self.statuses.get(pid)

    def stdout(self, pid: int) -> str:
        return self.stdout_by_pid.get(pid, "")

    def stderr(self, pid: int) -> str:
        return self.stderr_by_pid.get(pid, "")

    def kill(self, pid: int) -> None:
        self.killed.append(pid)

    def list_processes(self) -> list[WorkerSandboxProcess]:
        return list(self.processes)

    def cleanup(self) -> None:
        self.cleanup_count += 1
        self.cleanup_event.set()


@dataclass(slots=True)
class ProcessManagerFactory:
    manager: ProcessManager
    suspended: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)

    def create_process_manager(self, instance: WorkerContainerServiceInstance) -> ProcessManager:
        _ = instance
        return self.manager

    def suspend_process_streams(self, instance: WorkerContainerServiceInstance) -> None:
        self.suspended.append(instance.container_id)

    def resume_process_streams(self, instance: WorkerContainerServiceInstance) -> None:
        self.resumed.append(instance.container_id)


@dataclass(slots=True)
class RuntimeController:
    status_value: str = "running"
    exec_calls: list[tuple[str, list[str], list[str], str]] = field(default_factory=list)
    kill_calls: list[tuple[str, int, bool]] = field(default_factory=list)
    exec_response: ContainerExecResponse = field(default_factory=ContainerExecResponse)
    kill_error: RuntimeError | None = None
    exec_event: Event = field(default_factory=Event)

    def status(self, container_id: str) -> str:
        _ = container_id
        return self.status_value

    def exec_container(
        self,
        container_id: str,
        *,
        argv: list[str],
        env: list[str],
        cwd: str,
    ) -> ContainerExecResponse:
        self.exec_calls.append((container_id, argv, env, cwd))
        self.exec_event.set()
        return self.exec_response

    def kill_container(self, container_id: str, *, signal: int, force_delete: bool) -> None:
        self.kill_calls.append((container_id, signal, force_delete))
        if self.kill_error is not None:
            raise self.kill_error


@dataclass(slots=True)
class LogSink:
    entries: list[SandboxProcessLogEntry] = field(default_factory=list)

    def append_sandbox_process_log(self, entry: SandboxProcessLogEntry) -> None:
        self.entries.append(entry)


@dataclass(slots=True)
class CheckpointCreator:
    checkpoint_ids: list[str] = field(default_factory=lambda: ["checkpoint-1"])
    instances: list[str] = field(default_factory=list)
    requested_ids: list[str] = field(default_factory=list)

    def create_checkpoint(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        checkpoint_id: str = "",
    ) -> str:
        self.instances.append(instance.container_id)
        self.requested_ids.append(checkpoint_id)
        return checkpoint_id or self.checkpoint_ids.pop(0)


@dataclass(slots=True)
class ArchiveCreator:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def archive_container(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        image_id: str,
    ) -> list[ContainerArchiveResponse]:
        self.calls.append((instance.container_id, image_id))
        return [
            ContainerArchiveResponse(progress=25),
            ContainerArchiveResponse(done=True, success=True),
        ]


@dataclass(slots=True)
class NetworkPolicyUpdater:
    calls: list[tuple[str, bool, list[str]]] = field(default_factory=list)
    error: str = ""

    def update_network_permissions(
        self,
        container_id: str,
        *,
        block_network: bool,
        allow_list: list[str],
    ) -> None:
        self.calls.append((container_id, block_network, allow_list))
        if self.error:
            raise RuntimeError(self.error)


def test_worker_container_service_exec_persists_sandbox_process_logs(tmp_path: Path) -> None:
    store = _store(_instance(tmp_path))
    manager = ProcessManager(
        events=[
            SandboxProcessEvent(event_type=SandboxProcessEventType.Started, pid=321),
            SandboxProcessEvent(
                event_type=SandboxProcessEventType.Chunk,
                pid=321,
                seq=7,
                stream=SandboxLogStream.Stdout,
                data=b"hello from sandbox\n",
            ),
            SandboxProcessEvent(event_type=SandboxProcessEventType.Exited, pid=321, exit_code=0),
        ]
    )
    logs = LogSink()
    service = WorkerContainerService(
        instances=store,
        process_managers=ProcessManagerFactory(manager),
        logs=logs,
    )

    response = service.sandbox_exec(
        ContainerSandboxExecRequest(
            container_id="ctr-1",
            command="python app.py",
            cwd="/workspace",
            env={"A": "B"},
        )
    )

    assert response.ok
    assert response.pid == 321
    assert manager.cleanup_event.wait(timeout=1)
    assert manager.stream_calls == [(["python", "app.py"], "/workspace", ["BASE=1", "A=B"])]
    assert logs.entries[0].container_id == "ctr-1"
    assert logs.entries[0].line == "hello from sandbox\n"
    assert logs.entries[0].process_args == ["python", "app.py"]
    assert store.instances["ctr-1"].sandbox_process_manager_ready
    assert [
        item.msg for item in service.stream_logs(ContainerStreamLogsRequest(container_id="ctr-1"))
    ] == ["hello from sandbox\n"]


def test_worker_container_service_runtime_and_process_operations(tmp_path: Path) -> None:
    store = _store(_instance(tmp_path, sandbox_process_manager_ready=True))
    manager = ProcessManager(
        statuses={101: None, 102: 7},
        stdout_by_pid={101: "stdout"},
        stderr_by_pid={101: "stderr"},
        processes=[WorkerSandboxProcess(pid=101, command="python app.py")],
    )
    runtime = RuntimeController(
        exec_response=ContainerExecResponse(ok=True, exit_code=0, stdout="ok")
    )
    service = WorkerContainerService(
        instances=store,
        process_managers=ProcessManagerFactory(manager),
        runtime=runtime,
    )

    status = service.container_status(ContainerStatusRequest(container_id="ctr-1"))
    exec_response = service.container_exec(
        ContainerExecRequest(container_id="ctr-1", command="echo ok", env=("REQ=1",))
    )
    running = service.sandbox_status(ContainerSandboxStatusRequest(container_id="ctr-1", pid=101))
    exited = service.sandbox_status(ContainerSandboxStatusRequest(container_id="ctr-1", pid=102))
    stdout = service.sandbox_stdout(ContainerSandboxStdoutRequest(container_id="ctr-1", pid=101))
    stderr = service.sandbox_stderr(ContainerSandboxStderrRequest(container_id="ctr-1", pid=101))
    killed = service.sandbox_kill(ContainerSandboxKillRequest(container_id="ctr-1", pid=101))
    processes = service.sandbox_list_processes(
        ContainerSandboxListProcessesRequest(container_id="ctr-1")
    )
    container_kill = service.container_kill(ContainerKillRequest(container_id="ctr-1"))

    assert status.ok
    assert status.status == "running"
    assert exec_response.ok
    assert runtime.exec_calls[0] == (
        "ctr-1",
        ["sh", "-c", "echo ok"],
        ["BASE=1", "REQ=1"],
        "/workspace",
    )
    assert running.status == "running"
    assert running.exit_code == -1
    assert exited.status == "exited"
    assert exited.exit_code == 7
    assert stdout.stdout == "stdout"
    assert stderr.stderr == "stderr"
    assert killed.ok
    assert manager.killed == [101]
    assert processes.processes[0].pid == 101
    assert container_kill.ok
    assert runtime.kill_calls == [("ctr-1", 15, True)]


def test_supervisor_transport_authenticates_without_exposing_token_in_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, peer = socket.socketpair()

    def connect(
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
        *,
        all_errors: bool = False,
    ) -> socket.socket:
        _ = address, timeout, source_address, all_errors
        return client

    monkeypatch.setattr(socket, "create_connection", connect)
    token = "s" * 48
    transport = _SupervisorTransport.connect(
        "sandbox.internal",
        7111,
        token=token,
        timeout_seconds=0.1,
        retry=False,
        coordinator=_SupervisorConnectionCoordinator(),
    )
    request = SupervisorRequest(op="ready")

    try:
        transport.send(request)
        payload = SupervisorRequest.model_validate_json(peer.makefile("rb").readline())
    finally:
        transport.close()
        peer.close()

    assert payload.token == token
    assert token not in repr(request)
    assert token not in repr(transport)


def test_worker_container_service_kill_treats_missing_runtime_container_as_stopped(
    tmp_path: Path,
) -> None:
    service = WorkerContainerService(
        instances=_store(_instance(tmp_path)),
        runtime=RuntimeController(kill_error=RuntimeError("container does not exist")),
    )

    killed = service.container_kill(ContainerKillRequest(container_id="ctr-1"))

    assert killed.ok


def test_worker_container_service_file_operations_and_workspace_sync(tmp_path: Path) -> None:
    store = _store(_instance(tmp_path))
    service = WorkerContainerService(instances=store)

    created = service.sandbox_create_directory(
        ContainerSandboxCreateDirectoryRequest(
            container_id="ctr-1",
            container_path="data",
            mode=0o700,
        )
    )
    uploaded = service.sandbox_upload_file(
        ContainerSandboxUploadFileRequest(
            container_id="ctr-1",
            container_path="data/app.txt",
            data=b"hello old",
            mode=0o600,
        )
    )
    downloaded = service.sandbox_download_file(
        ContainerSandboxDownloadFileRequest(container_id="ctr-1", container_path="data/app.txt")
    )
    stat = service.sandbox_stat_file(
        ContainerSandboxStatFileRequest(container_id="ctr-1", container_path="data/app.txt")
    )
    listed = service.sandbox_list_files(
        ContainerSandboxListFilesRequest(container_id="ctr-1", container_path="data")
    )
    replaced = service.sandbox_replace_in_files(
        ContainerSandboxReplaceInFilesRequest(
            container_id="ctr-1",
            container_path="data",
            pattern="old",
            new_string="new",
        )
    )
    found = service.sandbox_find_in_files(
        ContainerSandboxFindInFilesRequest(
            container_id="ctr-1",
            container_path="data",
            pattern="new",
        )
    )
    sync_write = service.sync_workspace(
        SyncContainerWorkspaceRequest(
            container_id="ctr-1",
            operation=ContainerWorkspaceSyncOperation.Write,
            path="sync/a.txt",
            data=b"sync",
        )
    )
    sync_move = service.sync_workspace(
        SyncContainerWorkspaceRequest(
            container_id="ctr-1",
            operation=ContainerWorkspaceSyncOperation.Move,
            path="sync/a.txt",
            new_path="sync/b.txt",
        )
    )
    sync_delete = service.sync_workspace(
        SyncContainerWorkspaceRequest(
            container_id="ctr-1",
            operation=ContainerWorkspaceSyncOperation.Delete,
            path="sync/b.txt",
        )
    )
    deleted_file = service.sandbox_delete_file(
        ContainerSandboxDeleteFileRequest(container_id="ctr-1", container_path="data/app.txt")
    )
    deleted_dir = service.sandbox_delete_directory(
        ContainerSandboxDeleteDirectoryRequest(container_id="ctr-1", container_path="data")
    )

    assert created.ok
    assert uploaded.ok
    assert downloaded.data == b"hello old"
    assert stat.file_info.name == "app.txt"
    assert stat.file_info.permissions == 0o600
    assert [item.name for item in listed.files] == ["app.txt"]
    assert replaced.ok
    assert found.results[0].path == "workspace/data/app.txt"
    assert found.results[0].line == 1
    assert sync_write.ok
    assert sync_move.ok
    assert sync_delete.ok
    assert not (tmp_path / "workspace" / "sync" / "b.txt").exists()
    assert deleted_file.ok
    assert deleted_dir.ok


def test_worker_container_service_sandboxed_download_streams_from_supervisor(
    tmp_path: Path,
) -> None:
    payload = b"compose:\x00payload\n"
    manager = ProcessManager(
        events=[
            SandboxProcessEvent(event_type=SandboxProcessEventType.Started, pid=41),
            SandboxProcessEvent(
                event_type=SandboxProcessEventType.Chunk,
                pid=41,
                seq=1,
                stream=SandboxLogStream.Stdout,
                data=payload,
            ),
            SandboxProcessEvent(
                event_type=SandboxProcessEventType.Exited,
                pid=41,
                exit_code=0,
            ),
        ]
    )
    instance = _instance(
        tmp_path,
        runtime=OciRuntimeName.Runsc,
        sandbox_process_manager_ready=True,
    )
    service = WorkerContainerService(
        instances=_store(instance),
        process_managers=ProcessManagerFactory(manager),
    )

    response = service.sandbox_download_file(
        ContainerSandboxDownloadFileRequest(
            container_id="ctr-1",
            container_path="docker-compose.yml",
        )
    )

    assert response.ok
    assert response.data == payload
    assert manager.stream_calls == [
        (["cat", "/workspace/docker-compose.yml"], "/workspace", ["BASE=1"])
    ]
    assert manager.acknowledgements == [(41, 1, True)]
    assert manager.cleanup_count == 1


def test_worker_container_service_exposes_ports_and_updates_network(tmp_path: Path) -> None:
    store = _store(
        _instance(
            tmp_path,
            ports=[7111, 2222, 8080],
            workspace_id="workspace-1",
            machine_id="machine-1",
            worker_id="worker-1",
            pool="pool-1",
            route_local_target_host="agent.internal",
        )
    )
    network_policy = NetworkPolicyUpdater()
    service = WorkerContainerService(instances=store, network_policy=network_policy)

    exposed = service.sandbox_list_exposed_ports(
        ContainerSandboxListExposedPortsRequest(container_id="ctr-1")
    )
    response = service.sandbox_expose_port(
        ContainerSandboxExposePortRequest(container_id="ctr-1", port=9090)
    )
    listed_after_exposure = service.sandbox_list_exposed_ports(
        ContainerSandboxListExposedPortsRequest(container_id="ctr-1")
    )
    network = service.sandbox_update_network_permissions(
        ContainerSandboxUpdateNetworkPermissionsRequest(
            container_id="ctr-1",
            block_network=True,
            allow_list=("example.com",),
        )
    )

    assert exposed.ok
    assert exposed.ports == ()
    assert response.ok
    assert response.url == "http://127.0.0.1:9090"
    assert store.instances["ctr-1"].address_map[9090] == "127.0.0.1:9090"
    assert store.instances["ctr-1"].exposed_ports == [9090]
    assert listed_after_exposure.ok
    assert listed_after_exposure.ports == (9090,)
    assert network.ok
    assert network_policy.calls == [("ctr-1", True, ["example.com"])]
    assert store.instances["ctr-1"].network_blocked
    assert store.instances["ctr-1"].network_allow_list == ["example.com"]


def test_restored_worker_instance_lists_persisted_exposed_ports(tmp_path: Path) -> None:
    original = _instance(tmp_path, ports=[7111, 2222, 8080])
    original.exposed_ports = [18080]
    restored = WorkerContainerServiceInstance.model_validate_json(original.model_dump_json())
    service = WorkerContainerService(instances=_store(restored))

    response = service.sandbox_list_exposed_ports(
        ContainerSandboxListExposedPortsRequest(container_id="ctr-1")
    )

    assert response.ok
    assert response.ports == (18080,)


def test_worker_container_service_unexposes_only_requested_port(tmp_path: Path) -> None:
    store = _store(
        _instance(
            tmp_path,
            ports=[8080, 2222],
        )
    )
    instance = store.instances["ctr-1"]
    instance.address_map = {
        8080: "127.0.0.1:8080",
        2222: "127.0.0.1:2222",
    }
    instance.exposed_ports = [8080, 2222]
    service = WorkerContainerService(instances=store)

    response = service.sandbox_unexpose_port(
        ContainerSandboxUnexposePortRequest(container_id="ctr-1", port=2222)
    )

    assert response.ok
    assert store.instances["ctr-1"].address_map == {8080: "127.0.0.1:8080"}
    assert store.instances["ctr-1"].exposed_ports == [8080]


def test_worker_container_service_network_update_fails_without_policy_updater(
    tmp_path: Path,
) -> None:
    store = _store(_instance(tmp_path))
    service = WorkerContainerService(instances=store)

    response = service.sandbox_update_network_permissions(
        ContainerSandboxUpdateNetworkPermissionsRequest(
            container_id="ctr-1",
            block_network=True,
        )
    )

    assert not response.ok
    assert response.error_msg == "container network policy updater unavailable"
    assert not store.instances["ctr-1"].network_blocked


def test_worker_container_service_network_update_does_not_persist_after_policy_error(
    tmp_path: Path,
) -> None:
    store = _store(_instance(tmp_path))
    service = WorkerContainerService(
        instances=store,
        network_policy=NetworkPolicyUpdater(error="iptables failed"),
    )

    response = service.sandbox_update_network_permissions(
        ContainerSandboxUpdateNetworkPermissionsRequest(
            container_id="ctr-1",
            block_network=True,
        )
    )

    assert not response.ok
    assert response.error_msg == "iptables failed"
    assert not store.instances["ctr-1"].network_blocked


def _store(instance: WorkerContainerServiceInstance) -> LocalWorkerContainerInstanceStore:
    return LocalWorkerContainerInstanceStore(instances={instance.container_id: instance})


def _instance(
    root: Path,
    *,
    sandbox_process_manager_ready: bool = False,
    runtime: OciRuntimeName = OciRuntimeName.Runc,
    ports: list[int] | None = None,
    workspace_id: str = "workspace-1",
    app_id: str = "app-1",
    stub_id: str = "stub-1",
    worker_id: str = "worker-1",
    machine_id: str = "",
    pool: str = "",
    route_local_target_host: str = "",
) -> WorkerContainerServiceInstance:
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    return WorkerContainerServiceInstance(
        container_id="ctr-1",
        root_path=str(root),
        cwd="/workspace",
        runtime=runtime,
        env=["BASE=1"],
        ports=list(ports or []),
        sandbox_process_manager_ready=sandbox_process_manager_ready,
        workspace_id=workspace_id,
        app_id=app_id,
        stub_id=stub_id,
        worker_id=worker_id,
        machine_id=machine_id,
        pool=pool,
        route_local_target_host=route_local_target_host,
    )
