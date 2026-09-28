from uuid import UUID, uuid4

from api.server.services import ApiServices
from database.repositories.compute import (
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from fastapi.testclient import TestClient
from identity.auth import AuthService
from identity.platform import PlatformNamespaceService
from identity.users import UserService
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    UnitName,
)
from shared.http.fleet import FleetNodeListResponse, FleetSummaryResponse
from shared.identity import WorkspaceRecord
from shared.placement import Placement
from shared.timestamps import utc_now
from tests.workspaces import administrator_credential


def test_fleet_requires_administrator_without_workspace_membership(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    member = UserService(services.context).create(display_name="fleet-member")
    token, _ = AuthService(services.context).create_account_token(member.id, "fleet-member")
    for path in ("/api/v1/fleet", "/api/v1/fleet/nodes"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": f"Bearer {token}"}).status_code == 403
    admin_token, _ = administrator_credential(services.context, "fleet-admin")
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = client.get("/api/v1/fleet", headers=headers)
    assert response.status_code == 200, response.text
    assert FleetSummaryResponse.model_validate_json(response.content).plan is None
    assert client.get("/api/v1/fleet/nodes", headers=headers).status_code == 200


def test_fleet_inventory_pages_live_platform_nodes_and_excludes_customer_and_retired_nodes(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
) -> None:
    services, client = api_runtime
    workspace = PlatformNamespaceService(services.database).get()
    expected: set[str] = set()
    with services.database.session() as session:
        for platform in (True, False):
            pool_id = str(uuid4())
            pool = ComputeUnitRecord(
                id=pool_id,
                workspace_id=workspace.id if platform else api_workspace.id,
                name=UnitName(f"fleet-{pool_id}"),
                placement=Placement.platform() if platform else Placement.machine(pool_id),
                platform_fleet=platform,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                capacity_owner_id=pool_id,
                capacity_mode=ComputeCapacityMode.Pooled,
                provider_ref="aws",
                provider="aws",
                region="us-east-1",
                offer_id="m7i.xlarge",
                capability_key="fleet-acceptance",
                visibility=ComputeUnitVisibility.Internal
                if platform
                else ComputeUnitVisibility.Public,
                worker_cpu_millicores=4000,
                worker_memory_mib=16384,
            )
            ComputeUnitRepository(session).upsert(pool)
            for index, status in enumerate(("pending", "stopped", "terminating", "deleted")):
                identity = str(uuid4())
                ComputeProviderInstanceRepository(session).upsert(
                    ComputeProviderInstanceRecord(
                        id=identity,
                        provider="aws",
                        offer_id=f"fleet-offer-{identity}",
                        instance_type="m7i.xlarge",
                        instance_id=None if index == 0 else f"i-{identity}",
                        pool_id=pool.id,
                        status=status,
                        source="pooled",
                        provider_storage_destroyed_at=utc_now() if status == "deleted" else None,
                    )
                )
                if platform and status != "deleted":
                    expected.add(identity)
    token, _ = administrator_credential(services.context, "fleet-inventory-admin")
    headers = {"Authorization": f"Bearer {token}"}
    seen: list[str] = []
    cursor = ""
    for _ in range(4):
        response = client.get(
            "/api/v1/fleet/nodes",
            params={"limit": 1, **({"cursor": cursor} if cursor else {})},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        page = FleetNodeListResponse.model_validate_json(response.content)
        seen.extend(node.id for node in page.data)
        if not page.next:
            break
        assert UUID(page.next)
        cursor = page.next
    assert len(seen) == len(set(seen))
    assert set(seen) == expected
    assert client.get("/api/v1/fleet/nodes?cursor=invalid", headers=headers).status_code == 422
