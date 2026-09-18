from __future__ import annotations

import hashlib
import sys
from contextlib import ExitStack
from dataclasses import replace
from uuid import uuid4

from api.fastapi_app import create_app
from api.server.services import ApiServices
from compute.policy import WorkspaceComputePolicyService
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.repositories.orchestration import MachineRepository
from fastapi.testclient import TestClient
from httpx2 import Response
from identity.auth import AuthService
from pydantic import JsonValue, TypeAdapter
from shared.compute_fleet import Machine
from shared.compute_policy import (
    UnitName,
)
from shared.identity import TokenKind
from shared.placement import Placement
from storage.service import ObjectStorage
from tests.redis_fakes import FakeRedis
from tests.workspaces import owned_workspace

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_JSON_OBJECT_LIST: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(list[dict[str, JsonValue]])


def test_account_token_create_names_and_revokes(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
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
        revoked = client.post(f"/api/v1/tokens/{token_id}/revoke", headers=_auth(admin_token))
        assert revoked.status_code == 200
        token_items = _response_object_list(
            client.get("/api/v1/tokens", headers=_auth(admin_token)),
            "data",
        )
        names = [_required_string(item, "name") for item in token_items]
        assert "dashboard" not in names


def test_first_run_bootstrap_is_explicit_and_routes_fail_closed(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        client = client_stack.enter_context(TestClient(create_app(isolated_services)))

        assert client.get("/auth/bootstrap").status_code == 404
        assert client.post("/auth/bootstrap", json={"name": "root"}).status_code == 404
        assert client.get("/api/v1/tokens").status_code == 401

        admin_token = _offline_admin_token(isolated_services, "first-run")

        assert client.get("/api/v1/tokens", headers=_auth(admin_token)).status_code == 200
        admin_list = client.get("/api/v1/workspaces", headers=_auth(admin_token))
        assert admin_list.status_code == 200
        assert _response_object_list(admin_list, "workspaces")

        workspace_token, workspace_token_record = AuthService(
            isolated_services.context
        ).create_token(
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
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    _, client = api_runtime

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
) -> None:
    with ExitStack() as client_stack:
        client = client_stack.enter_context(TestClient(create_app(isolated_services)))
        admin_token = _offline_admin_token(isolated_services, "compute-projection")
        workspace = owned_workspace(
            ControlPlaneService(
                isolated_services.context,
                placement_resolver=WorkspaceComputePolicyService(isolated_services.context),
            ),
            "compute-team",
        )

        isolated_services.compute.create_unit(UnitName("default-pool"))
        isolated_services.compute.create_unit(UnitName("team-pool"), workspace=workspace.id)
        with isolated_services.context.database.session() as session:
            MachineRepository(session).upsert(
                Machine(id=str(uuid4()), placement=Placement.machine("team-pool")),
                workspace_id=workspace.id,
            )

        pools = client.get(
            f"/api/v1/units?workspace={workspace.id}",
            headers=_auth(admin_token),
        )
        machines = client.get(
            f"/api/v1/machines?workspace={workspace.id}",
            headers=_auth(admin_token),
        )

        assert pools.status_code == 200
        assert [
            _required_string(pool, "name") for pool in _response_object_list(pools, "pools")
        ] == ["team-pool"]
        assert machines.status_code == 200
        assert [
            _required_string(machine, "provider")
            for machine in _response_object_list(machines, "machines")
        ] == ["local"]

        workspace_token, _record = AuthService(isolated_services.context).create_token(
            "workspace-user",
            kind=TokenKind.Workspace,
        )
        denied = client.get(
            f"/api/v1/units?workspace={workspace.id}",
            headers=_auth(workspace_token),
        )
        assert denied.status_code == 403


def test_object_upload_authorizes_before_creating_or_completing_a_claim(
    isolated_services: ApiServices,
) -> None:
    from database.repositories.storage import ObjectRepository
    from tests.fakes import FakeObjectClient

    object_storage = ObjectStorage(isolated_services.context, object_client=FakeObjectClient())
    gateway = replace(isolated_services.gateway_service, object_storage=object_storage)
    token, _ = AuthService(isolated_services.context).create_token(
        "source-upload", kind=TokenKind.Workspace
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    other = owned_workspace(
        ControlPlaneService(
            isolated_services.context,
            placement_resolver=WorkspaceComputePolicyService(isolated_services.context),
        ),
        "other-upload-owner",
    )
    request: dict[str, JsonValue] = {
        "object_metadata": {"name": "source.zip", "size": 3},
        "hash": hashlib.sha256(b"abc").hexdigest(),
    }
    with TestClient(create_app(isolated_services, gateway_service=gateway)) as client:
        assert client.post("/gateway/objects/uploads", json=request).status_code == 401
        started = client.post("/gateway/objects/uploads", json=request, headers=_auth(token))
        assert started.status_code == 200
        payload = _response_object(started)
        object_id = _required_string(payload, "object_id")
        upload = payload["upload"]
        assert isinstance(upload, dict)
        claim_id = _required_string(upload, "claim_id")
        denied = client.post(
            f"/gateway/objects/uploads/{object_id}/complete?workspace={other.id}",
            json={"claim_id": claim_id, "etag": "part"},
            headers=_auth(token),
        )
        assert denied.status_code == 403
        with isolated_services.context.database.session() as session:
            assert ObjectRepository(session).get(object_id, workspace_id=workspace_id) is None
        aborted = client.post(
            f"/gateway/objects/uploads/{object_id}/abort",
            json={"claim_id": claim_id},
            headers=_auth(token),
        )
        assert aborted.status_code == 204
        with isolated_services.context.database.session() as session:
            assert (
                ObjectRepository(session).get(
                    object_id, workspace_id=workspace_id, include_operations=True
                )
                is None
            )


def test_gateway_container_attach_emits_sse(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
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
        assert attach.status_code == 200
        assert ": connected" in attach.text
        assert _response_object(attach_snapshot)["input_supported"] is False
        assert _response_string(attach_snapshot, "attach_contract") == "sse-output-only"


def test_gateway_task_routes_do_not_cross_workspace_boundaries(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    control = ControlPlaneService(
        services.context, placement_resolver=WorkspaceComputePolicyService(services.context)
    )
    workspace_a = owned_workspace(control, "workspace-a")
    workspace_b = owned_workspace(control, "workspace-b")
    token_a, _ = AuthService(services.context).create_token(
        "workspace-a",
        kind=TokenKind.Workspace,
        workspace_id=workspace_a.id,
    )
    task_b = services.tasks.create("tenant-b-task", workspace_id=workspace_b.id)

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


def _offline_admin_token(services: ApiServices, suffix: str) -> str:
    return (
        AuthService(services.context)
        .bootstrap_administrator(
            request_id=f"bootstrap:gateway-{suffix}",
        )
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
