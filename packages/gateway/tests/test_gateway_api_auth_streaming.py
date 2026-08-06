from __future__ import annotations

import hashlib
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from fastapi.testclient import TestClient
from gateway.events import GatewayRequestEventMiddleware
from httpx2 import Response
from identity.auth import AuthService
from pydantic import JsonValue, TypeAdapter
from scheduler.state import (
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
)
from shared.compute_policy import (
    MachinePool,
    UnitName,
)
from shared.contracts import ContractModel
from shared.events import EventLevel
from shared.identity import AuthScope, TokenKind
from shared.worker_events import GATEWAY_REQUEST_EVENT_ACTION
from starlette.types import Receive, Scope, Send
from storage.service import ObjectStorage
from storage_client.s3 import S3ObjectInfo
from tests.redis_fakes import FakeRedis
from worker.container_client import models

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_JSON_OBJECT_LIST: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(list[dict[str, JsonValue]])

_GATEWAY_RPC_PATHS = {
    "/gateway/agents/join",
    "/gateway/agents/leave",
    "/gateway/agents/routes",
    "/gateway/agents/routes/status",
    "/gateway/agents/stream",
    "/gateway/agents/stream/events",
    "/gateway/agents/tailnet-device",
    "/gateway/agents/telemetry",
    "/gateway/agents/telemetry/stream",
    "/gateway/agents/transport-credential",
    "/gateway/authorize",
    "/gateway/client-manifests",
    "/gateway/containers/attach",
    "/gateway/containers/attach/stream",
    "/gateway/containers/checkpoint",
    "/gateway/containers/sync-workspace",
    "/gateway/deployments/resolve-target",
    "/gateway/objects/download",
    "/gateway/objects/head",
    "/gateway/objects/stream",
    "/gateway/provider-nodes/enroll",
    "/gateway/sign-payload",
    "/gateway/stubs/deploy",
    "/gateway/stubs/get-or-create",
    "/gateway/stubs/url",
    "/gateway/tasks/end",
    "/gateway/tasks/log",
    "/gateway/tasks/start",
}


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


def test_workspace_token_create_names_and_deletes(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    admin_token = _offline_admin_token(isolated_services, "workspace-token-crud")

    created = client.post(
        "/api/v1/tokens",
        headers=_auth(admin_token),
        json={"name": "dashboard"},
    )
    assert created.status_code == 201
    payload = _response_object(created)
    record = _JSON_OBJECT.validate_python(payload["record"])
    assert record["name"] == "dashboard"
    assert payload["token"]

    token_id = _required_string(record, "id")
    deleted = client.delete(f"/api/v1/tokens/{token_id}", headers=_auth(admin_token))
    assert deleted.status_code == 204
    token_items = _response_object_list(
        client.get("/api/v1/tokens", headers=_auth(admin_token)),
        "tokens",
    )
    names = [_required_string(item, "name") for item in token_items]
    assert "dashboard" not in names


def test_first_run_bootstrap_is_explicit_and_routes_fail_closed(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    assert client.get("/auth/bootstrap").status_code == 404
    assert client.post("/auth/bootstrap", json={"name": "root"}).status_code == 404
    assert client.get("/api/v1/tokens").status_code == 401

    admin_token = _offline_admin_token(isolated_services, "first-run")

    assert client.get("/api/v1/tokens", headers=_auth(admin_token)).status_code == 200
    admin_list = client.get("/api/v1/workspaces", headers=_auth(admin_token))
    assert admin_list.status_code == 200
    assert _response_object_list(admin_list, "workspaces")

    workspace_token, workspace_token_record = AuthService(isolated_services.context).create_token(
        "workspace",
        kind=TokenKind.Workspace,
    )
    denied = client.post(
        "/api/v1/workspaces",
        json={"name": "blocked"},
        headers=_auth(workspace_token),
    )
    assert denied.status_code == 403
    listed = client.get("/api/v1/workspaces", headers=_auth(workspace_token))
    assert listed.status_code == 200
    assert [
        _required_string(item, "id") for item in _response_object_list(listed, "workspaces")
    ] == [workspace_token_record.workspace_id]


def test_agent_routes_use_service_owned_join_and_agent_tokens(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    rejected_join = client.post(
        "/gateway/agents/join",
        json={"join_token": "invalid-join-token", "machine_fingerprint": "machine-1"},
    )
    rejected_stream = client.post(
        "/gateway/agents/stream",
        json={"agent_token": "invalid-agent-token"},
    )
    rejected_telemetry = client.post(
        "/gateway/agents/telemetry",
        json={"agent_token": "invalid-agent-token"},
    )

    assert rejected_join.status_code == 400
    assert "join token" in _response_string(rejected_join, "detail")
    assert rejected_stream.status_code == 200
    # A stale token is rejected and carries no work back. The rest of the
    # payload is defaults, so matching it whole would fail whenever the contract
    # gains a field without the rejection itself changing.
    rejected_stream_body = _response_object(rejected_stream)
    assert rejected_stream_body["ok"] is False
    assert rejected_stream_body["err_msg"] == "agent token is no longer current"
    assert rejected_stream_body["routes"] == []
    assert rejected_stream_body["slots"] == []
    assert rejected_telemetry.status_code == 200
    assert _response_object(rejected_telemetry) == {
        "ok": False,
        "err_msg": "invalid agent token",
    }


def test_compute_gateway_projections_honor_admin_workspace_override(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    admin_token = _offline_admin_token(isolated_services, "compute-projection")
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("compute-team")

    isolated_services.compute.create_unit(UnitName("default-pool"))
    isolated_services.compute.create_unit(UnitName("team-pool"), workspace=workspace.id)
    isolated_services.compute.create_machine(pool=MachinePool("team-pool"), workspace=workspace.id)

    pools = client.get(
        f"/api/v1/units?workspace={workspace.id}",
        headers=_auth(admin_token),
    )
    machines = client.get(
        f"/api/v1/machines?workspace={workspace.id}",
        headers=_auth(admin_token),
    )

    assert pools.status_code == 200
    assert [_required_string(pool, "name") for pool in _response_object_list(pools, "pools")] == [
        "team-pool"
    ]
    assert machines.status_code == 200
    assert [
        _required_string(machine, "pool") for machine in _response_object_list(machines, "machines")
    ] == ["team-pool"]

    workspace_token, _record = AuthService(isolated_services.context).create_token(
        "workspace-user",
        kind=TokenKind.Workspace,
    )
    denied = client.get(
        f"/api/v1/units?workspace={workspace.id}",
        headers=_auth(workspace_token),
    )
    assert denied.status_code == 403


def test_gateway_raw_object_upload_uses_file_spool_and_download_redirect(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    object_client = _ObjectClient()
    object_storage = ObjectStorage(isolated_services.context, object_client=object_client)
    gateway_service = replace(
        isolated_services.gateway_service,
        compute_state=RedisComputeStateRepository(_redis()),
        object_storage=object_storage,
    )
    client = client_stack.enter_context(
        TestClient(
            create_app(
                isolated_services,
                gateway_service=gateway_service,
            )
        )
    )
    admin_token = _offline_admin_token(isolated_services, "object-stream")
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    physical_key = object_storage.physical_key_for_workspace(
        workspace_id,
        bucket="default",
        key="payload.bin",
    )
    physical_bucket = object_storage.physical_bucket("default")
    content = b"streamed object content"
    digest = hashlib.sha256(content).hexdigest()

    uploaded = client.post(
        f"/gateway/objects/stream?bucket=default&name=payload.bin&hash={digest}&size={len(content)}",
        content=content,
        headers=_auth(admin_token)
        | {
            "content-type": "application/octet-stream",
            "x-object-meta-purpose": "source",
        },
    )
    repeated = client.post(
        f"/gateway/objects/stream?bucket=default&name=payload.bin&hash={digest}&size={len(content)}",
        content=content,
        headers=_auth(admin_token) | {"content-type": "application/octet-stream"},
    )
    redirected = client.get(
        "/gateway/objects/download?bucket=default&key=payload.bin",
        headers=_auth(admin_token),
        follow_redirects=False,
    )

    assert uploaded.status_code == 200
    assert _response_string(uploaded, "object_id") == _response_string(repeated, "object_id")
    assert object_client.put_file_payloads == [content]
    assert object_client.put_bytes_called is False
    assert object_client.exists(physical_key, bucket=physical_bucket)
    assert redirected.status_code == 307
    assert redirected.headers["location"].endswith(f"/{physical_bucket}/{physical_key}")
    object_client.delete(physical_key, bucket=physical_bucket)
    assert not object_client.exists(physical_key, bucket=physical_bucket)

    repaired = client.post(
        f"/gateway/objects/stream?bucket=default&name=payload.bin&hash={digest}&size={len(content)}",
        content=content,
        headers=_auth(admin_token) | {"content-type": "application/octet-stream"},
    )

    assert repaired.status_code == 200
    assert _response_string(repaired, "object_id") == _response_string(uploaded, "object_id")
    assert object_client.put_file_payloads == [content, content]
    assert object_client.exists(physical_key, bucket=physical_bucket)


def test_gateway_object_stream_requires_auth_and_exact_content_proof(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    object_client = _ObjectClient()
    gateway_service = replace(
        isolated_services.gateway_service,
        compute_state=RedisComputeStateRepository(_redis()),
        object_storage=ObjectStorage(isolated_services.context, object_client=object_client),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, gateway_service=gateway_service))
    )
    admin_token = _offline_admin_token(isolated_services, "object-stream-proof")
    content = b"first payload"
    digest = hashlib.sha256(content).hexdigest()
    path = (
        f"/gateway/objects/stream?bucket=default&name=proof.bin&hash={digest}&size={len(content)}"
    )

    assert client.post(path, content=content).status_code == 401
    assert (
        client.post(
            "/gateway/objects/create",
            headers=_auth(admin_token),
            json={},
        ).status_code
        == 404
    )
    assert (
        client.post(
            path,
            content=content,
            headers=_auth(admin_token) | {"content-length": str(len(content) + 1)},
        ).status_code
        == 400
    )
    assert (
        client.post(
            path.replace("bucket=default", "bucket=platform-secrets"),
            content=content,
            headers=_auth(admin_token),
        ).status_code
        == 400
    )

    wrong_hash = hashlib.sha256(b"different").hexdigest()
    assert (
        client.post(
            f"/gateway/objects/stream?bucket=default&name=proof.bin&hash={wrong_hash}&size={len(content)}",
            content=content,
            headers=_auth(admin_token),
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/gateway/objects/stream"
            f"?bucket=default&name=proof.bin&hash={digest}&size={len(content) + 1}",
            content=content,
            headers=_auth(admin_token),
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/gateway/objects/stream"
            f"?bucket=default&name=proof.bin&hash={digest}&size={len(content) - 1}",
            content=content,
            headers=_auth(admin_token),
        ).status_code
        == 400
    )

    created = client.post(path, content=content, headers=_auth(admin_token))
    replacement = b"replacement payload"
    replacement_hash = hashlib.sha256(replacement).hexdigest()
    conflicted = client.post(
        "/gateway/objects/stream"
        f"?bucket=default&name=proof.bin&hash={replacement_hash}&size={len(replacement)}",
        content=replacement,
        headers=_auth(admin_token),
    )

    assert created.status_code == 200
    assert conflicted.status_code == 409
    assert object_client.put_file_payloads == [content]


def test_gateway_attach_and_agent_streams_emit_sse(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    gateway_service = replace(
        isolated_services.gateway_service,
        compute_state=RedisComputeStateRepository(_redis()),
    )
    client = client_stack.enter_context(
        TestClient(
            create_app(
                isolated_services,
                gateway_service=gateway_service,
            )
        )
    )
    admin_token = _offline_admin_token(isolated_services, "container-attach")
    container = isolated_services.containers.run(
        "attach-target",
        "python",
        [sys.executable, "-c", "print('attach-output')"],
    )

    attach = client.get(
        f"/gateway/containers/attach/stream?container_id={container.id}&max_idle_polls=1",
        headers=_auth(admin_token),
    )
    attach_snapshot = client.post(
        "/gateway/containers/attach",
        json={"container_id": container.id},
        headers=_auth(admin_token),
    )
    machine_token, _ = AuthService(isolated_services.context).create_token(
        "agent-machine",
        scopes=[AuthScope.Machine.value],
        kind=TokenKind.WorkerPrivate,
    )
    agent = client.get(
        "/gateway/agents/stream/events?agent_token=missing&max_events=1",
        headers=_auth(machine_token),
    )

    assert attach.status_code == 200
    assert ": connected" in attach.text
    assert _response_object(attach_snapshot)["input_supported"] is False
    assert _response_string(attach_snapshot, "attach_contract") == "sse-output-only"
    assert agent.status_code == 200
    assert "event: agent.error" in agent.text


def test_gateway_task_routes_do_not_cross_workspace_boundaries(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace_a = control.upsert_workspace("workspace-a")
    workspace_b = control.upsert_workspace("workspace-b")
    token_a, _ = AuthService(isolated_services.context).create_token(
        "workspace-a",
        kind=TokenKind.Workspace,
        workspace_id=workspace_a.id,
    )
    task_b = isolated_services.tasks.create("tenant-b-task", workspace_id=workspace_b.id)
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    listed = client.get("/api/v1/tasks", headers=_auth(token_a))
    stopped = client.delete(
        "/api/v1/tasks",
        params={"task_ids": task_b.id},
        headers=_auth(token_a),
    )
    fetched = client.get(f"/api/v1/tasks/{task_b.id}", headers=_auth(token_a))

    assert listed.status_code == 200
    assert _response_object_list(listed, "data") == []
    assert stopped.status_code == 200
    assert _response_object(stopped)["stopped"] == []
    assert _response_object(stopped)["skipped"] == [task_b.id]
    assert fetched.status_code == 404


def _services_with_redis(
    isolated_services: ApiServices,
    redis: RedisClient,
    request: pytest.FixtureRequest,
) -> ApiServices:
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=redis,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services


def test_gateway_request_events_persist_only_server_errors(
    isolated_services: ApiServices,
) -> None:
    async def failing_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 502, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = GatewayRequestEventMiddleware(failing_app, isolated_services.events)
    client = TestClient(middleware, raise_server_exceptions=False)
    client.get("/deploy-target")
    events = isolated_services.events.list()

    request_event = next(event for event in events if event.action == GATEWAY_REQUEST_EVENT_ACTION)
    assert request_event.level is EventLevel.Error
    assert request_event.data["status_code"] == 502
    assert request_event.data["path"] == "/deploy-target"


def _offline_admin_token(services: ApiServices, suffix: str) -> str:
    return (
        AuthService(services.context)
        .bootstrap_admin_token(request_id=f"bootstrap:gateway-{suffix}")
        .token
    )


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _response_object(response: Response) -> dict[str, JsonValue]:
    return _JSON_OBJECT.validate_python(response.json())


def _response_object_list(response: Response, key: str) -> list[dict[str, JsonValue]]:
    return _JSON_OBJECT_LIST.validate_python(_response_object(response)[key])


def _response_string(response: Response, key: str) -> str:
    return _required_string(_response_object(response), key)


def _required_string(payload: dict[str, JsonValue], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        msg = f"response field {key!r} must be a string"
        raise AssertionError(msg)
    return value


def _redis() -> RedisClient:
    return RedisClient(FakeRedis(), key_prefix="test")


@dataclass
class _TransportCall:
    method: models.ContainerServiceMethod
    request: ContractModel
    timeout_seconds: float | None


@dataclass
class _RecordingTransport:
    unary_calls: list[_TransportCall] = field(default_factory=list)

    def unary(
        self,
        method: models.ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> ContractModel:
        self.unary_calls.append(_TransportCall(method, request, timeout_seconds))
        if method is models.ContainerServiceMethod.ContainerSyncWorkspace:
            assert isinstance(request, models.SyncContainerWorkspaceRequest)
            return models.SyncContainerWorkspaceResponse(path=request.path)
        if method is models.ContainerServiceMethod.ContainerStatus:
            return models.ContainerStatusResponse(status="running")
        if method is models.ContainerServiceMethod.ContainerCheckpoint:
            assert isinstance(request, models.ContainerCheckpointRequest)
            return models.ContainerCheckpointResponse(checkpoint_id=request.checkpoint_id)
        raise AssertionError(f"unexpected container service method: {method}")

    def stream(
        self,
        method: models.ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> list[ContractModel]:
        self.unary_calls.append(_TransportCall(method, request, timeout_seconds))
        return []


@dataclass
class _RecordingTransportFactory:
    transport: _RecordingTransport

    def create_transport(
        self,
        options: models.ContainerClientConnectionOptions,
    ) -> _RecordingTransport:
        _ = options
        return self.transport


@dataclass
class _SchedulerContainers:
    state: SchedulerContainerState | None = None
    worker_address: SchedulerContainerAddress | None = None

    def get_container_state(self, container_id: str) -> SchedulerContainerState | None:
        if self.state is not None and self.state.container_id == container_id:
            return self.state
        return None

    def get_worker_address(self, container_id: str) -> SchedulerContainerAddress | None:
        if self.worker_address is not None and self.worker_address.container_id == container_id:
            return self.worker_address
        return None

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        return SchedulerContainerAddressMap(container_id=container_id)


@dataclass
class _ObjectClient:
    put_file_payloads: list[bytes] = field(default_factory=list)
    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    object_metadata: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    put_bytes_called: bool = False

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        del key, data, bucket, content_type, metadata
        self.put_bytes_called = True
        raise AssertionError("raw upload should not use put_bytes")

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        del content_type
        payload = Path(source).read_bytes()
        self.put_file_payloads.append(payload)
        location = (bucket or "default", key)
        self.objects[location] = payload
        self.object_metadata[location] = dict(metadata or {})
        return S3ObjectInfo(bucket=bucket or "default", key=key, size=len(payload))

    def read_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        return self.objects[(bucket or "default", key)]

    def download_file(
        self,
        key: str,
        target: str | Path,
        *,
        bucket: str | None = None,
    ) -> S3ObjectInfo:
        target_bucket = bucket or "default"
        payload = self.read_bytes(key, bucket=target_bucket)
        Path(target).write_bytes(payload)
        return S3ObjectInfo(bucket=target_bucket, key=key, size=len(payload))

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        data = self.read_bytes(key, bucket=bucket)
        location = (bucket or "default", key)
        return S3ObjectInfo(
            bucket=location[0],
            key=key,
            size=len(data),
            metadata=self.object_metadata.get(location, {}),
        )

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        return (bucket or "default", key) in self.objects

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str:
        del bucket, expires_seconds, content_length, content_type
        return f"https://objects.test/put/{key}"

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        del expires_seconds
        return f"https://objects.test/{bucket or 'default'}/{key}"

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        location = (bucket or "default", key)
        self.objects.pop(location, None)
        self.object_metadata.pop(location, None)
