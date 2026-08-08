from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.compute_policy import MachinePool, UnitName
from shared.http.compute import UnitMachineListResponse
from shared.identity import TokenKind
from tests.service_fixtures import administrator_credential


def test_self_hosted_collection_is_static_workspace_scoped_and_excludes_managed_pools(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    raw_token, _record = AuthService(isolated_services.context).create_token(
        "self-hosted-list",
        kind=TokenKind.Workspace,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}

    initial = client.get("/api/v1/machines/pool?pool=self-hosted&limit=250", headers=headers)
    assert initial.status_code == 200
    assert UnitMachineListResponse.model_validate_json(initial.content) == UnitMachineListResponse()

    isolated_services.compute.create_unit(
        UnitName("managed-pool"),
        pool=MachinePool("lazycloud"),
        provider="local",
    )
    isolated_services.compute.create_machine(pool=MachinePool("lazycloud"), provider="local")
    after_managed_machine = client.get("/api/v1/machines/pool?pool=self-hosted", headers=headers)

    assert after_managed_machine.status_code == 200
    assert (
        UnitMachineListResponse.model_validate_json(after_managed_machine.content)
        == UnitMachineListResponse()
    )


def test_canonical_capacity_routes_enforce_workspace_and_admin_authority(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    auth = AuthService(isolated_services.context)
    workspace_token, _workspace_record = auth.create_token(
        "capacity-workspace",
        kind=TokenKind.Workspace,
    )
    admin_token, _admin_record = administrator_credential(isolated_services, "capacity-admin")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    workspace_headers = {"Authorization": f"Bearer {workspace_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    assert client.post("/api/v1/workers/missing/cordon").status_code == 401
    assert (
        client.post(
            "/api/v1/workers/missing/cordon",
            headers=workspace_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/workers/missing/cordon",
            headers=admin_headers,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            "/api/v1/units/missing",
            headers=workspace_headers,
        ).status_code
        == 403
    )
    assert (
        client.delete(
            "/api/v1/units/missing",
            headers=admin_headers,
        ).status_code
        == 404
    )
