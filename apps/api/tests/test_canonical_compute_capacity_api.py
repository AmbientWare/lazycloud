from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from compute.offers import ComputeOffer
from fastapi.testclient import TestClient
from identity.auth import AuthService
from pydantic import JsonValue
from shared.http.compute import PoolCapacityResponse, PoolMachineListResponse
from shared.identity import TokenKind
from tests.provider_fixtures import configure_test_provider


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

    initial = client.get("/api/v1/machines/self-hosted?limit=250", headers=headers)
    assert initial.status_code == 200
    assert PoolMachineListResponse.model_validate_json(initial.content) == PoolMachineListResponse()

    isolated_services.compute.create_pool("managed-pool", provider="local")
    isolated_services.compute.create_machine(pool="managed-pool", provider="local")
    after_managed_machine = client.get("/api/v1/machines/self-hosted", headers=headers)

    assert after_managed_machine.status_code == 200
    assert (
        PoolMachineListResponse.model_validate_json(after_managed_machine.content)
        == PoolMachineListResponse()
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
    admin_token, _admin_record = auth.create_token(
        "capacity-admin",
        kind=TokenKind.Admin,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    workspace_headers = {"Authorization": f"Bearer {workspace_token}"}

    assert client.get("/api/v1/pools/example/offers").status_code == 401
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
            headers={"Authorization": f"Bearer {admin_token}"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/pools/missing/join-token",
            headers=workspace_headers,
            json={},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/pools/missing/join-command",
            headers=workspace_headers,
            json={},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/pools/missing/machines",
            headers=workspace_headers,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            "/api/v1/pools/missing/join-token",
            headers=workspace_headers,
        ).status_code
        == 404
    )


def test_capacity_public_route_adds_nodes_and_patch_extends_the_aggregate(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    configure_test_provider(
        isolated_services,
        "route-provider",
        [
            ComputeOffer(
                id="route-cpu",
                provider="route-provider",
                instance_type="cpu-large",
                region="lab",
                cpu_millicores=4000,
                memory_mb=8192,
                hourly_cost_micros=100_000,
                available=2,
            )
        ],
    )
    raw_token, _record = AuthService(isolated_services.context).create_token(
        "capacity-route",
        kind=TokenKind.Workspace,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}
    launch_payload: dict[str, JsonValue] = {
        "provider": ["route-provider"],
        "node_count": 1,
        "ttl": "1h",
        "max_spend": 1.0,
    }

    first_response = client.post(
        "/api/v1/pools/route-pool/capacity",
        headers=headers,
        json=launch_payload,
    )
    second_response = client.post(
        "/api/v1/pools/route-pool/capacity",
        headers=headers,
        json=launch_payload,
    )

    assert first_response.status_code == 201, first_response.text
    assert second_response.status_code == 201, second_response.text
    first = PoolCapacityResponse.model_validate_json(first_response.content)
    second = PoolCapacityResponse.model_validate_json(second_response.content)
    assert first.reserved_nodes == 1
    assert second.reserved_nodes == 2
    assert second.max_spend_micros == 2_000_000

    at_limit_response = client.post(
        "/api/v1/pools/route-pool/capacity",
        headers=headers,
        json=launch_payload,
    )
    assert at_limit_response.status_code == 409
    assert "maximum of 2 workers" in at_limit_response.json()["detail"]

    extension_response = client.patch(
        "/api/v1/pools/route-pool/capacity",
        headers=headers,
        json={"ttl": "2h", "max_spend": 2.0},
    )
    assert extension_response.status_code == 200, extension_response.text
    extended = PoolCapacityResponse.model_validate_json(extension_response.content)
    assert extended.expires_at is not None
    assert second.expires_at is not None
    assert extended.expires_at > second.expires_at
    assert extended.reserved_nodes == 2
