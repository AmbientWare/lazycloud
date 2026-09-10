from __future__ import annotations

import socket
import threading
from collections.abc import Iterable
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import execution.shells.service as shell_service_module
import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.orchestration import ContainerRepository
from execution.containers.service import ContainerService
from execution.pods.service import PodControlService
from execution.shells.planning import SHELL_WORKER_PORT
from execution.shells.proxy import ShellBackendTarget
from execution.shells.service import (
    ShellControlService,
    ShellTicketCompensationStatus,
)
from fastapi.testclient import TestClient
from identity.auth import AuthService
from identity.websocket_tickets import ShellWebSocketAudience, WebSocketTicketService
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.fleet import SchedulerContainerStatus
from scheduler.state import (
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
    SchedulerWorkerRequest,
)
from shared.bytes_transport import encode_bytes
from shared.container_requests import WORKER_USER_CODE_VOLUME
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.http.pods import (
    CreatePodResponse,
    PodSandboxDownloadFileResponse,
    PodSandboxExecRequest,
    PodSandboxExecResponse,
    PodSandboxListFilesResponse,
    PodSandboxUploadFileBody,
    PodSandboxUploadFileResponse,
)
from shared.http.pods import (
    PodSandboxUpdateNetworkPermissionsResponse as HttpPodSandboxUpdateNetworkPermissionsResponse,
)
from shared.shell_protocol import ShellFrameType, encode_shell_frame
from shared.workload_keys import pod_keep_warm_lock_key
from starlette.websockets import WebSocketDisconnect
from tests.real_redis import RealRedisActors
from tests.scheduler_composition import services_with_redis_container_control
from tests.url_constants import TEST_URL
from worker.container_client import models
from worker.container_client.control import ContainerServiceTransport
from worker.container_client.models import (
    ContainerClientConnectionOptions,
    ContainerExecResponse,
    ContainerSandboxDownloadFileResponse,
    ContainerSandboxExecResponse,
    ContainerSandboxExposePortResponse,
    ContainerSandboxFileInfo,
    ContainerSandboxListFilesResponse,
    ContainerSandboxStatusResponse,
    ContainerSandboxUnexposePortResponse,
    ContainerSandboxUpdateNetworkPermissionsResponse,
    ContainerSandboxUploadFileResponse,
    ContainerServiceMethod,
    ContainerServicePayload,
    ContainerStatusResponse,
)
from worker.container_client.scheduler import SchedulerContainerClientFactory

BASE_URL = TEST_URL


def test_ephemeral_pod_create_overrides_command_returns_url_and_expires(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    with ExitStack() as client_stack:
        scheduler = _RecordingScheduler()
        redis = real_redis_actors.client()
        real_services = services_with_redis_container_control(isolated_services, redis)
        services = replace(
            real_services,
            containers=replace(real_services.containers, scheduler=scheduler),
        )
        control = ControlPlaneService(services.context)
        stub = control.create_stub(
            "ephemeral-web",
            kind=StubKind.Pod,
            public=True,
            config={
                "image": {"image_id": "image-pod"},
                "runtime": {"keep_warm": 600},
                "command": ["python", "authored.py"],
                "ports": {"8080": 8080},
            },
        )
        service = PodControlService(services, redis=redis)
        client = client_stack.enter_context(TestClient(create_app(services, pod_service=service)))
        headers = _auth_headers(services)

        response = client.post(
            "/api/v1/pods",
            json={
                "stub_id": stub.id,
                "command": ["python", "override.py"],
                "timeout_seconds": 30,
                "external_url": BASE_URL,
            },
            headers=headers,
        )

        assert response.status_code == 200
        created = CreatePodResponse.model_validate_json(response.content)
        assert created.url == f"https://{stub.id}-8080.lazycloud.test"
        assert created.timeout_seconds == 30
        assert created.expires_at is not None
        assert scheduler.requests[0].payload["entrypoint"] == ["python", "override.py"]
        assert scheduler.requests[0].payload["cwd"] == WORKER_USER_CODE_VOLUME
        container = services.containers.get(created.container_id)
        assert container.timeout_seconds == 30
        assert container.expires_at is not None
        assert service.expire_pods(now=container.expires_at - timedelta(seconds=1)) == []
        expired = service.expire_pods(now=container.expires_at)
        assert [item.id for item in expired] == [container.id]
        assert services.containers.get(container.id).status is ContainerStatus.Stopped

        no_timeout_response = client.post(
            "/api/v1/pods",
            json={
                "stub_id": stub.id,
                "timeout_seconds": -1,
                "external_url": BASE_URL,
            },
            headers=headers,
        )
        no_timeout = CreatePodResponse.model_validate_json(no_timeout_response.content)
        assert no_timeout.timeout_seconds == -1
        assert no_timeout.expires_at is None
        assert scheduler.requests[1].payload["entrypoint"] == ["python", "authored.py"]
        never_lock = pod_keep_warm_lock_key(stub.workspace_id, stub.id, no_timeout.container_id)
        assert service.redis.exists(service.redis.key(never_lock))

        scalable_response = client.post(
            "/api/v1/pods",
            json={
                "stub_id": stub.id,
                "timeout_seconds": 0,
                "external_url": BASE_URL,
            },
            headers=headers,
        )
        scalable = CreatePodResponse.model_validate_json(scalable_response.content)
        scalable_lock = pod_keep_warm_lock_key(stub.workspace_id, stub.id, scalable.container_id)
        assert not service.redis.exists(service.redis.key(scalable_lock))


def test_pod_api_schedules_container_and_routes_exec_and_files_to_worker(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        scheduler = _RecordingScheduler()
        isolated_services.containers.scheduler = scheduler
        control = ControlPlaneService(isolated_services.context)
        stub = control.create_stub(
            "remote-sandbox",
            kind=StubKind.Sandbox,
            config={
                "image": {
                    "image_id": "image-remote",
                },
                "runtime": {
                    "cpu": 1.5,
                    "memory": "256Mi",
                    "gpu": ["T4"],
                    "gpu_count": 1,
                    "keep_warm": 60,
                    "runtime_class": "runsc",
                    "docker_enabled": True,
                    "block_network": False,
                    "allow_list": ["10.0.0.0/8"],
                    "preemptible": True,
                    "pool_selector": "gpu-pool",
                },
                "env": {"APP_ENV": "test"},
                "command": ["python", "-m", "http.server"],
                "ports": {"8080": 8080},
                "secrets": ["API_KEY"],
            },
        )
        scheduler_containers = _FakeSchedulerContainers()
        transport = _RecordingTransport()
        transport.responses = {
            ContainerServiceMethod.ContainerSandboxExec: ContainerSandboxExecResponse(pid=42),
            ContainerServiceMethod.ContainerSandboxStatus: ContainerSandboxStatusResponse(
                status="complete",
                exit_code=0,
            ),
            ContainerServiceMethod.ContainerSandboxUploadFile: ContainerSandboxUploadFileResponse(),
            ContainerServiceMethod.ContainerSandboxDownloadFile: (
                ContainerSandboxDownloadFileResponse(data=b"remote-data")
            ),
            ContainerServiceMethod.ContainerSandboxListFiles: ContainerSandboxListFilesResponse(
                files=(
                    ContainerSandboxFileInfo(
                        name="app.py",
                        mode=0o100644,
                        size=11,
                        permissions=0o644,
                    ),
                )
            ),
            ContainerServiceMethod.ContainerSandboxUpdateNetworkPermissions: (
                ContainerSandboxUpdateNetworkPermissionsResponse()
            ),
        }
        transport_factory = _RecordingTransportFactory(transport)
        pod_service = PodControlService(
            isolated_services,
            redis=isolated_services.redis(),
            scheduler_containers=scheduler_containers,
            container_clients=SchedulerContainerClientFactory(
                scheduler_containers=scheduler_containers,
                transport_factory=transport_factory,
            ),
        )
        client = client_stack.enter_context(
            TestClient(create_app(isolated_services, pod_service=pod_service))
        )
        headers = _auth_headers(isolated_services)

        created_response = client.post("/api/v1/pods", json={"stub_id": stub.id}, headers=headers)
        created = CreatePodResponse.model_validate_json(created_response.content)
        container_id = created.container_id
        assert container_id
        container = isolated_services.containers.get(container_id)
        scheduler_containers.state = SchedulerContainerState(
            container_id=container_id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        )
        scheduler_containers.worker_address = SchedulerContainerAddress(
            container_id=container_id,
            address="worker.internal:9001",
        )
        scheduler_containers.address_map = SchedulerContainerAddressMap(
            container_id=container_id,
            address_map={8080: "10.0.0.5:8080"},
        )

        exec_http_response = client.post(
            f"/api/v1/pods/{container_id}/exec",
            json={
                "command": "echo remote",
                "env": {"EXTRA": "1"},
                "cwd": "/workspace",
            },
            headers=headers,
        )
        exec_response = PodSandboxExecResponse.model_validate_json(exec_http_response.content)
        upload_body = PodSandboxUploadFileBody(
            container_path="/workspace/app.py",
            value_base64=encode_bytes(b"remote-data"),
        )
        upload_http_response = client.post(
            f"/api/v1/pods/{container_id}/files/upload",
            json=upload_body.model_dump(mode="json"),
            headers=headers,
        )
        upload_response = PodSandboxUploadFileResponse.model_validate_json(
            upload_http_response.content
        )
        download_http_response = client.get(
            f"/api/v1/pods/{container_id}/files/download",
            params={"container_path": "/workspace/app.py"},
            headers=headers,
        )
        download_response = PodSandboxDownloadFileResponse.model_validate_json(
            download_http_response.content
        )
        files_http_response = client.get(
            f"/api/v1/pods/{container_id}/files",
            params={"container_path": "/workspace"},
            headers=headers,
        )
        files_response = PodSandboxListFilesResponse.model_validate_json(
            files_http_response.content
        )
        network_response = client.post(
            f"/api/v1/pods/{container_id}/network/update",
            json={"allow_list": ["192.168.0.0/16"]},
            headers=headers,
        )
        persisted_network = client.get(
            f"/api/v1/pods/{container_id}/network",
            headers=headers,
        )

        assert container.status is ContainerStatus.Pending
        assert exec_response.pid == 42
        persisted_container = isolated_services.containers.get(container_id)
        assert persisted_container.status is ContainerStatus.Running
        assert persisted_container.worker_id is None
        assert persisted_container.runtime_worker_id == "worker-1"
        assert upload_response == PodSandboxUploadFileResponse()
        assert download_response.value_base64
        assert files_response.files[0].name == "app.py"
        assert network_response.status_code == 200
        persisted_network_body = HttpPodSandboxUpdateNetworkPermissionsResponse.model_validate_json(
            persisted_network.content
        )
        assert persisted_network_body.block_network is False
        assert persisted_network_body.allow_list == ["192.168.0.0/16"]


def test_existing_container_shell_reuses_credentials_through_worker_client(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("shell-target", kind=StubKind.Pod)
    container = _create_running_container(isolated_services, stub.id, stub.workspace_id)
    scheduler_containers = _FakeSchedulerContainers(
        state=SchedulerContainerState(
            container_id=container.id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        ),
        worker_address=SchedulerContainerAddress(
            container_id=container.id,
            address="worker.internal:9001",
        ),
        address_map=SchedulerContainerAddressMap(
            container_id=container.id,
            address_map={SHELL_WORKER_PORT: "shell.example:2222"},
        ),
    )
    transport = _RecordingTransport(
        responses={
            ContainerServiceMethod.ContainerSandboxExposePort: ContainerSandboxExposePortResponse(
                url="http://shell.example"
            ),
            ContainerServiceMethod.ContainerExec: ContainerExecResponse(pid=7),
        }
    )
    service = ShellControlService(
        isolated_services,
        scheduler_containers=scheduler_containers,
        container_clients=SchedulerContainerClientFactory(
            scheduler_containers=scheduler_containers,
            transport_factory=_RecordingTransportFactory(transport),
        ),
        backend_connector=_ready_shell_connector,
    )

    first = service.create_shell_in_existing_container(
        workspace_id=stub.workspace_id,
        container_id=container.id,
    )
    second = service.create_shell_in_existing_container(
        workspace_id=stub.workspace_id,
        container_id=container.id,
    )

    assert first.stub_id == stub.id
    assert first.username
    assert first.password
    assert second == first


def test_existing_container_shell_rejects_unrelated_listener_and_rolls_back_port(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("shell-target", kind=StubKind.Pod)
    container = _create_running_container(isolated_services, stub.id, stub.workspace_id)
    scheduler_containers = _FakeSchedulerContainers(
        state=SchedulerContainerState(
            container_id=container.id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        ),
        worker_address=SchedulerContainerAddress(
            container_id=container.id,
            address="worker.internal:9001",
        ),
        address_map=SchedulerContainerAddressMap(
            container_id=container.id,
            address_map={SHELL_WORKER_PORT: "unrelated.internal:2222"},
        ),
    )
    transport = _RecordingTransport(
        responses={
            ContainerServiceMethod.ContainerExec: ContainerExecResponse(pid=7),
            ContainerServiceMethod.ContainerSandboxExposePort: ContainerSandboxExposePortResponse(
                url="http://unrelated.internal:2222"
            ),
            ContainerServiceMethod.ContainerSandboxUnexposePort: (
                ContainerSandboxUnexposePortResponse()
            ),
        }
    )
    monkeypatch.setattr(shell_service_module, "SHELL_SERVER_READY_TIMEOUT_SECONDS", 0.01)
    service = ShellControlService(
        isolated_services,
        scheduler_containers=scheduler_containers,
        container_clients=SchedulerContainerClientFactory(
            scheduler_containers=scheduler_containers,
            transport_factory=_RecordingTransportFactory(transport),
        ),
        backend_connector=_unrelated_shell_connector,
    )

    with pytest.raises(UpstreamUnavailableError, match="shell frame payload exceeds"):
        service.create_shell_in_existing_container(
            workspace_id=stub.workspace_id,
            container_id=container.id,
        )

    assert (container.id, SHELL_WORKER_PORT) not in transport.exposed_ports


def test_existing_container_ticket_failure_unpublishes_listener_idempotently(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("shell-cleanup", kind=StubKind.Pod)
    container = _create_running_container(isolated_services, stub.id, stub.workspace_id)
    scheduler_containers = _FakeSchedulerContainers(
        state=SchedulerContainerState(
            container_id=container.id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        ),
        worker_address=SchedulerContainerAddress(
            container_id=container.id,
            address="worker.internal:9001",
        ),
    )
    transport = _RecordingTransport(
        responses={
            ContainerServiceMethod.ContainerSandboxUnexposePort: (
                ContainerSandboxUnexposePortResponse()
            ),
        }
    )
    service = ShellControlService(
        isolated_services,
        scheduler_containers=scheduler_containers,
        container_clients=SchedulerContainerClientFactory(
            scheduler_containers=scheduler_containers,
            transport_factory=_RecordingTransportFactory(transport),
        ),
    )

    transport.exposed_ports.add((container.id, SHELL_WORKER_PORT))
    first = service.compensate_existing_container_ticket_failure(
        workspace_id=stub.workspace_id,
        container_id=container.id,
    )
    second = service.compensate_existing_container_ticket_failure(
        workspace_id=stub.workspace_id,
        container_id=container.id,
    )

    assert first.status is ShellTicketCompensationStatus.Cleaned
    assert second.status is ShellTicketCompensationStatus.Cleaned
    assert (container.id, SHELL_WORKER_PORT) not in transport.exposed_ports
    assert isolated_services.containers.get(container.id).status is ContainerStatus.Running


def test_standalone_ticket_failure_stops_once_and_terminal_retry_is_idempotent(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("standalone-cleanup", kind=StubKind.Pod)
    container = _create_running_container(isolated_services, stub.id, stub.workspace_id)
    service = ShellControlService(isolated_services)

    first = service.compensate_standalone_ticket_failure(
        workspace_id=stub.workspace_id,
        container_id=container.id,
    )
    second = service.compensate_standalone_ticket_failure(
        workspace_id=stub.workspace_id,
        container_id=container.id,
    )

    assert first.status is ShellTicketCompensationStatus.Cleaned
    assert first.terminal_status is ContainerStatus.Stopped
    assert second.status is ShellTicketCompensationStatus.Cleaned
    assert isolated_services.containers.get(container.id).status is ContainerStatus.Stopped


def test_standalone_ticket_cleanup_failure_preserves_truth_and_records_safe_event(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("standalone-cleanup-failure", kind=StubKind.Pod)
    container = _create_running_container(isolated_services, stub.id, stub.workspace_id)

    def fail_stop(_container_service: ContainerService, _container_id: str) -> ContainerRecord:
        raise RuntimeError("private scheduler endpoint")

    monkeypatch.setattr(type(isolated_services.containers), "stop", fail_stop)
    service = ShellControlService(isolated_services)

    result = service.compensate_standalone_ticket_failure(
        workspace_id=stub.workspace_id,
        container_id=container.id,
    )

    assert result.status is ShellTicketCompensationStatus.Failed
    assert result.terminal_status is None
    assert result.failure_recorded
    assert isolated_services.containers.get(container.id).status is ContainerStatus.Running
    events = isolated_services.events.list(
        workspace_id=stub.workspace_id,
        resource_type="container",
        resource_id=container.id,
        actions=["shell.ticket.compensation_failed"],
    )
    assert len(events) == 1
    assert "scheduler" not in events[0].message
    assert "endpoint" not in events[0].message


def test_sandbox_exec_waits_for_worker_address_before_dial(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("delayed-sandbox", kind=StubKind.Sandbox)
    container = _create_running_container(isolated_services, stub.id, stub.workspace_id)
    scheduler_containers = _FakeSchedulerContainers(
        state=SchedulerContainerState(
            container_id=container.id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        ),
        worker_address=SchedulerContainerAddress(
            container_id=container.id,
            address="worker.internal:9001",
        ),
        worker_address_after_calls=2,
    )
    transport = _RecordingTransport(
        response_sequences={
            ContainerServiceMethod.ContainerStatus: [
                ContainerStatusResponse(status="created"),
                ContainerStatusResponse(status="running"),
            ],
        },
        responses={
            ContainerServiceMethod.ContainerSandboxExec: ContainerSandboxExecResponse(pid=42),
        },
    )
    service = PodControlService(
        isolated_services,
        redis=isolated_services.redis(),
        scheduler_containers=scheduler_containers,
        container_clients=SchedulerContainerClientFactory(
            scheduler_containers=scheduler_containers,
            transport_factory=_RecordingTransportFactory(transport),
        ),
        poll_interval_seconds=0,
        container_connect_timeout_seconds=1,
    )

    response = service.sandbox_exec(
        container.id,
        PodSandboxExecRequest(
            command="echo ready",
            cwd="/workspace",
        ),
    )

    assert response == PodSandboxExecResponse(pid=42)
    assert isolated_services.containers.get(container.id).status is ContainerStatus.Running


def test_sandbox_connect_surfaces_terminal_scheduler_state_as_conflict(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("stopped-sandbox", kind=StubKind.Sandbox)
    container = _create_running_container(isolated_services, stub.id, stub.workspace_id)
    scheduler_containers = _FakeSchedulerContainers(
        state=SchedulerContainerState(
            container_id=container.id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            worker_id="worker-1",
            status=SchedulerContainerStatus.Stopping,
        )
    )
    service = PodControlService(
        isolated_services,
        redis=isolated_services.redis(),
        scheduler_containers=scheduler_containers,
        poll_interval_seconds=0,
        container_connect_timeout_seconds=1,
    )

    with pytest.raises(ConflictError, match=rf"container {container.id} is stopping"):
        service.sandbox_connect(container.id)


def test_shell_websocket_proxies_bidirectional_terminal_bytes(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        control = ControlPlaneService(isolated_services.context)
        stub = control.create_stub("interactive-shell", kind=StubKind.Pod)
        container = _create_running_container(isolated_services, stub.id, stub.workspace_id)
        echo_server = _EchoServer()
        echo_server.start()
        scheduler_containers = _FakeSchedulerContainers(
            state=SchedulerContainerState(
                container_id=container.id,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
                worker_id="worker-1",
                status=SchedulerContainerStatus.Running,
            ),
            address_map=SchedulerContainerAddressMap(
                container_id=container.id,
                address_map={2222: echo_server.address},
            ),
        )
        shell_service = ShellControlService(
            isolated_services,
            scheduler_containers=scheduler_containers,
            container_clients=SchedulerContainerClientFactory(
                scheduler_containers=scheduler_containers,
                transport_factory=_RecordingTransportFactory(_RecordingTransport()),
            ),
            # The websocket route resolves its backend on the event loop, so a
            # service without these reaches it with no way to route.
            async_database=isolated_services.require_async_io().database,
            async_scheduler_containers=_FakeAsyncShellContainers(scheduler_containers),
        )
        client = client_stack.enter_context(
            TestClient(create_app(isolated_services, shell_service=shell_service))
        )
        headers = _auth_headers(isolated_services)
        token = AuthService(isolated_services.context).authenticate_header(
            headers["Authorization"],
            allow_if_no_tokens=False,
        )
        assert token is not None
        ticket = WebSocketTicketService(
            isolated_services.context,
            isolated_services.redis_client,
        ).mint_shell_ticket(
            token,
            audience=ShellWebSocketAudience(
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                container_id=container.id,
            ),
        )

        try:
            with client.websocket_connect(
                f"/api/v1/shells/id/{stub.id}/{container.id}/ws",
                headers=headers,
            ) as websocket:
                assert websocket.receive_text() == "OK"
                websocket.send_bytes(b"ping")
                assert websocket.receive_bytes() == b"ping"
            with client.websocket_connect(
                f"/api/v1/shells/id/{stub.id}/{container.id}/ws?ticket={ticket}",
            ) as websocket:
                assert websocket.receive_text() == "OK"
                websocket.send_bytes(b"ticket")
                assert websocket.receive_bytes() == b"ticket"
            with (
                pytest.raises(WebSocketDisconnect) as replayed,
                client.websocket_connect(
                    f"/api/v1/shells/id/{stub.id}/{container.id}/ws?ticket={ticket}",
                ),
            ):
                pass
            assert replayed.value.code == 1008
        finally:
            echo_server.close()


def test_shell_websocket_rejects_long_lived_query_credentials_before_backend(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        client = client_stack.enter_context(TestClient(create_app(isolated_services)))
        headers = _auth_headers(isolated_services)
        bearer = headers["Authorization"].removeprefix("Bearer ")

        for query in (f"token={bearer}", f"authorization=Bearer%20{bearer}", "ticket=unknown"):
            with (
                pytest.raises(WebSocketDisconnect) as closed,
                client.websocket_connect(
                    f"/api/v1/shells/id/stub-1/container-1/ws?{query}",
                ),
            ):
                pass
            assert closed.value.code == 1008


@dataclass(slots=True)
class _RecordingTransport:
    responses: dict[ContainerServiceMethod, ContainerServicePayload] = field(default_factory=dict)
    response_sequences: dict[ContainerServiceMethod, list[ContainerServicePayload]] = field(
        default_factory=dict
    )
    exposed_ports: set[tuple[str, int]] = field(default_factory=set)

    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerServicePayload:
        if method is ContainerServiceMethod.ContainerSandboxExposePort:
            assert isinstance(request, models.ContainerSandboxExposePortRequest)
            self.exposed_ports.add((request.container_id, request.port))
        elif method is ContainerServiceMethod.ContainerSandboxUnexposePort:
            assert isinstance(request, models.ContainerSandboxUnexposePortRequest)
            self.exposed_ports.discard((request.container_id, request.port))
        sequence = self.response_sequences.get(method)
        if sequence:
            return sequence.pop(0)
        if method is ContainerServiceMethod.ContainerStatus:
            return self.responses.get(
                method,
                ContainerStatusResponse(status="running"),
            )
        return self.responses[method]

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterable[ContainerServicePayload]:
        raise AssertionError(f"unexpected streaming request: {method}")


@dataclass(slots=True)
class _RecordingTransportFactory:
    transport: _RecordingTransport
    options: list[ContainerClientConnectionOptions] = field(default_factory=list)

    def create_transport(
        self,
        options: ContainerClientConnectionOptions,
    ) -> ContainerServiceTransport:
        self.options.append(options)
        return self.transport


@dataclass(slots=True)
class _FakeSchedulerContainers:
    state: SchedulerContainerState | None = None
    worker_address: SchedulerContainerAddress | None = None
    worker_address_after_calls: int = 0
    worker_address_calls: int = 0
    address_map: SchedulerContainerAddressMap = field(
        default_factory=lambda: SchedulerContainerAddressMap(container_id="")
    )

    def get_container_state(self, container_id: str) -> SchedulerContainerState | None:
        if self.state is None or self.state.container_id != container_id:
            return None
        return self.state

    def get_worker_address(self, container_id: str) -> SchedulerContainerAddress | None:
        self.worker_address_calls += 1
        if self.worker_address_calls <= self.worker_address_after_calls:
            return None
        if self.worker_address is None or self.worker_address.container_id != container_id:
            return None
        return self.worker_address

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        if self.address_map.container_id == container_id:
            return self.address_map
        return SchedulerContainerAddressMap(container_id=container_id)


@dataclass(slots=True)
class _FakeAsyncShellContainers:
    """The same answers as the synchronous fake, on the event loop.

    The websocket shell route resolves its backend asynchronously, so a service
    built without this reaches the route with nothing to route through and the
    connection closes on "asynchronous shell routing is not configured". Reading
    from the synchronous fake keeps one source of truth for both paths.
    """

    containers: _FakeSchedulerContainers

    async def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        return self.containers.get_container_address_map(container_id)


@dataclass(slots=True)
class _RecordingScheduler:
    requests: list[SchedulerWorkerRequest] = field(default_factory=list)

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        _ = ready_at
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
            reason="queued",
        )


class _EchoServer:
    def __init__(self) -> None:
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.tcp_address = self.socket.getsockname()
        self.address = f"127.0.0.1:{self.tcp_address[1]}"
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.closed.set()
        with suppress(OSError):
            socket.create_connection(self.tcp_address, timeout=0.1).close()
        self.socket.close()
        self.thread.join(timeout=1)
        assert not self.thread.is_alive(), "shell echo server did not stop"

    def _serve(self) -> None:
        while not self.closed.is_set():
            try:
                connection = self.socket.accept()[0]
            except OSError:
                return
            with connection:
                while not self.closed.is_set():
                    data = connection.recv(4096)
                    if not data:
                        break
                    connection.sendall(data)


def _ready_shell_connector(_target: ShellBackendTarget) -> socket.socket:
    client, server = socket.socketpair()

    def respond() -> None:
        with server:
            server.recv(4096)
            server.sendall(encode_shell_frame(ShellFrameType.Ready.value))

    threading.Thread(target=respond, daemon=True).start()
    return client


def _unrelated_shell_connector(_target: ShellBackendTarget) -> socket.socket:
    client, server = socket.socketpair()

    def respond() -> None:
        with server:
            server.recv(4096)
            server.sendall(b"HTTP/1.1 200 OK\r\n\r\n")

    threading.Thread(target=respond, daemon=True).start()
    return client


def _create_running_container(
    isolated_services: ApiServices,
    stub_id: str,
    workspace_id: str,
) -> ContainerRecord:
    container = ContainerRecord(
        id="33333333-3333-4333-8333-333333333333",
        name=f"container-{stub_id}",
        image="container-test",
        command=["sleep", "infinity"],
        workspace_id=workspace_id,
        stub_id=stub_id,
        status=ContainerStatus.Running,
    )
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).records.upsert(
            container,
            key=container.id,
            workspace_id=workspace_id,
            name=container.name,
            status=container.status.value,
        )
    return container


def _auth_headers(services: ApiServices) -> dict[str, str]:
    token = (
        AuthService(services.context)
        .bootstrap_administrator(request_id="bootstrap:pod-shell-remote")
        .token
    )
    return {"Authorization": f"Bearer {token}"}
