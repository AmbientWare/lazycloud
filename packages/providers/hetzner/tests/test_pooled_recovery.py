from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from api.server.services import ApiServices
from compute.offers import ComputeOffer
from compute.providers import ProviderUnitBootstrap, ProviderUnitRequest
from coordination.request_cooldown import RedisRequestCooldown
from database.repositories.compute import ComputeUnitRepository
from database.repositories.provider_launches import ProviderNodeLaunchRepository
from provider_hetzner.client import HetznerClient, PrimaryIP, Server
from provider_hetzner.identity import provider_label
from provider_hetzner.pooled_provider import HetznerNodeImage, HetznerPooledProvider
from pydantic import BaseModel, SecretStr
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
)


class _IPUpdate(BaseModel):
    auto_delete: bool
    labels: dict[str, str]


def test_observation_does_not_mutate_and_ensure_recovers_unbound_nodes(
    isolated_services: ApiServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = isolated_services
    unit_id = str(uuid4())
    with services.context.database.session() as session:
        pool = ComputeUnitRepository(session).upsert(
            ComputeUnitRecord(
                id=unit_id,
                workspace_id=services.context.default_workspace_id(session),
                name=UnitName("hetzner-recovery"),
                pool=MachinePool("lazycloud"),
                capacity_owner_id=unit_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                provider="hetzner",
                provider_ref="hetzner:test",
                platform_fleet=True,
                capacity_mode=ComputeCapacityMode.Pooled,
                visibility=ComputeUnitVisibility.Internal,
                region="ash",
                offer_id="ash:ccx13",
                capability_key="hetzner:ash:ccx13:amd64:runsc",
                desired_machines=2,
                max_machines=2,
            )
        )
    request = ProviderUnitRequest(
        workspace_id=pool.workspace_id,
        unit_id=pool.id,
        unit_name=pool.name,
        provider_ref=pool.provider_ref,
        provider_connection_id=None,
        generation=pool.generation,
        desired_machines=2,
        max_machines=2,
        offer=ComputeOffer(
            id=pool.offer_id,
            provider=pool.provider_ref,
            instance_type="ccx13",
            region="ash",
            capacity_mode=ComputeCapacityMode.Pooled,
        ),
        bootstrap=ProviderUnitBootstrap(
            control_plane_url="https://control.example.com",
            enrollment_request_id=pool.id,
            agent_version="test",
            agent_sha256="a" * 64,
            agent_binary_url="https://control.example.com/agent",
        ),
    )
    launches = [
        services.provider_node_launches.prepare(request, f"slot-{index}") for index in (1, 2)
    ]
    nodes = [
        Server.model_validate(
            {
                "id": index,
                "name": launch.server_name,
                "status": "running",
                "created": datetime.now(UTC),
                "labels": {
                    "lazycloud-managed": "true",
                    "lazycloud-unit": pool.id,
                    "lazycloud-provider": provider_label(pool.provider_ref),
                    "lazycloud-launch": launch.launch_id,
                    "lazycloud-generation": str(pool.generation),
                    "lazycloud-release": "a" * 63,
                },
                "location": {"name": "ash", "description": "Ashburn", "network_zone": "us-east"},
                "server_type": {
                    "id": 1,
                    "name": "ccx13",
                    "cores": 2,
                    "memory": 8,
                    "disk": 80,
                    "architecture": "x86",
                    "cpu_type": "dedicated",
                    "prices": [],
                },
                "public_net": {"ipv4": {"id": index, "ip": f"192.0.2.{index}"}},
                "volumes": [],
            }
        )
        for index, launch in enumerate(launches, start=1)
    ]
    addresses = {
        index: PrimaryIP(
            id=index,
            ip=f"192.0.2.{index}",
            assignee_id=index,
            auto_delete=False,
            labels={"owner-note": "preserve"},
        )
        for index in (1, 2)
    }

    def response(transport: httpx.HTTPTransport, incoming: httpx.Request) -> httpx.Response:
        del transport
        path = incoming.url.path
        if path == "/v1/servers" and incoming.method == "GET":
            return httpx.Response(
                200,
                json={
                    "servers": [node.model_dump(mode="json") for node in nodes],
                    "meta": {"pagination": {"next_page": None}},
                },
            )
        if path == "/v1/primary_ips" and incoming.method == "GET":
            tagged = [
                address.model_dump(mode="json")
                for address in addresses.values()
                if address.labels.get("lazycloud-unit") == pool.id
            ]
            return httpx.Response(
                200, json={"primary_ips": tagged, "meta": {"pagination": {"next_page": None}}}
            )
        if path.startswith("/v1/primary_ips/"):
            ip_id = int(path.rsplit("/", 1)[1])
            if incoming.method == "PUT":
                updated = _IPUpdate.model_validate_json(incoming.content)
                addresses[ip_id] = addresses[ip_id].model_copy(update=updated.model_dump())
            return httpx.Response(
                200, json={"primary_ip": addresses[ip_id].model_dump(mode="json")}
            )
        raise AssertionError(f"unexpected provider operation: {incoming.method} {path}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", response)
    adapter = HetznerPooledProvider(
        provider_ref=pool.provider_ref,
        client=HetznerClient(
            SecretStr("test-token"), RedisRequestCooldown(services.redis_client, pool.provider_ref)
        ),
        images_by_location={"ash": HetznerNodeImage(image_id=1, recipe_sha256="a" * 64)},
        usd_per_currency_unit=Decimal(1),
        primary_ipv4_hourly_micros=1,
        launch_credentials=services.provider_node_launches,
    )
    observed = adapter.describe_unit(request)
    assert observed.observed_machines == 2
    assert all(item.booted_template_version == "a" * 63 for item in observed.instances)
    assert observed.current_template_version != "a" * 63
    upgraded = adapter.describe_unit(
        request.model_copy(
            update={"bootstrap": request.bootstrap.model_copy(update={"agent_sha256": "b" * 64})}
        )
    )
    assert upgraded.current_template_version != observed.current_template_version
    assert upgraded.instances == observed.instances
    with services.context.database.session() as session:
        for launch in launches:
            recorded = ProviderNodeLaunchRepository(session).get(launch.launch_id)
            assert recorded is not None
            assert recorded.provider_instance_id is None
    assert all(not address.auto_delete for address in addresses.values())

    assert adapter.ensure_unit(request).observed_machines == 2
    with services.context.database.session() as session:
        for index, launch in enumerate(launches, start=1):
            recorded = ProviderNodeLaunchRepository(session).get(launch.launch_id)
            assert recorded is not None
            assert recorded.provider_instance_id == str(index)
    assert all(address.auto_delete for address in addresses.values())
    assert all(address.labels["lazycloud-unit"] == pool.id for address in addresses.values())
    assert all(address.labels["owner-note"] == "preserve" for address in addresses.values())
