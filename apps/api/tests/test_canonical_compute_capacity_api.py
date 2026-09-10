from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import AuthService, TokenIssuer
from shared.compute_policy import MachinePool, UnitName
from shared.http.compute import UnitMachineListResponse
from shared.identity import TokenKind, WorkspaceRecord
from tests.workspaces import administrator_credential, workspace_owner_user_id


def test_self_hosted_collection_is_account_scoped_and_excludes_managed_pools(
    isolated_services: ApiServices,
) -> None:
    """A person reads their own hardware; a workspace credential carries no account."""
    with ExitStack() as client_stack:
        with isolated_services.context.database.session() as session:
            workspace_id = isolated_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
        issuer = TokenIssuer(isolated_services.context)
        with isolated_services.context.database.session() as session:
            account_token, _account_record = issuer.issue_for_user(
                session,
                "self-hosted-list",
                user_id=user_id,
            )
        issuer.committed()
        workspace_token, _workspace_record = AuthService(isolated_services.context).create_token(
            "self-hosted-workspace",
            kind=TokenKind.Workspace,
        )
        client = client_stack.enter_context(TestClient(create_app(isolated_services)))
        headers = {"Authorization": f"Bearer {account_token}"}

        initial = client.get("/api/v1/machines/self-hosted?limit=250", headers=headers)
        assert initial.status_code == 200
        assert (
            UnitMachineListResponse.model_validate_json(initial.content)
            == UnitMachineListResponse()
        )

        isolated_services.compute.create_unit(
            UnitName("managed-pool"),
            pool=MachinePool("lazycloud"),
            provider="local",
        )
        isolated_services.compute.create_machine(pool=MachinePool("lazycloud"), provider="local")
        after_managed_machine = client.get("/api/v1/machines/self-hosted", headers=headers)

        assert after_managed_machine.status_code == 200
        assert (
            UnitMachineListResponse.model_validate_json(after_managed_machine.content)
            == UnitMachineListResponse()
        )
        assert (
            client.get(
                "/api/v1/machines/self-hosted",
                headers={"Authorization": f"Bearer {workspace_token}"},
            ).status_code
            == 403
        )


def test_canonical_capacity_routes_enforce_workspace_and_admin_authority(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
) -> None:
    services, client = api_runtime
    auth = AuthService(services.context)
    workspace_token, _workspace_record = auth.create_token(
        "capacity-workspace",
        kind=TokenKind.Workspace,
        workspace_id=api_workspace.id,
    )
    admin_token, _admin_record = administrator_credential(services.context, "capacity-admin")
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
