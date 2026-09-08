from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from api.server.services import ApiServices
from compute.offers import ComputeOffer
from compute.providers import ProviderCapacityPhase, ProviderUnitBootstrap, ProviderUnitRequest
from database.repositories.compute import ComputeUnitRepository
from provider_hyperstack.client import (
    Environment,
    Flavor,
    HyperstackClient,
    HyperstackError,
    Server,
)
from provider_hyperstack.pooled_provider import HyperstackDeployment, HyperstackPooledProvider
from pydantic import BaseModel, SecretStr
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_policy import ComputeCapacityMode, ComputeUnitRecord, MachinePool, UnitName


class _Creation(BaseModel):
    name: str
    labels: tuple[str, ...]


def test_ambiguous_create_is_fenced_and_late_instance_is_deleted(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = isolated_services
    unit_id = str(uuid4())
    with services.context.database.session() as session:
        unit = ComputeUnitRepository(session).upsert(
            ComputeUnitRecord(
                id=unit_id,
                workspace_id=services.context.default_workspace_id(session),
                name=UnitName("hyperstack-recovery"),
                pool=MachinePool("lazycloud"),
                capacity_owner_id=unit_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                provider="hyperstack",
                provider_ref="hyperstack:test",
                platform_fleet=True,
                capacity_mode=ComputeCapacityMode.Pooled,
                region="US-1",
                offer_id="US-1:n3-A100-SXM4x8",
                desired_machines=1,
                max_machines=1,
            )
        )
    request = ProviderUnitRequest(
        workspace_id=unit.workspace_id,
        unit_id=unit.id,
        unit_name=unit.name,
        provider_ref=unit.provider_ref,
        provider_connection_id=None,
        generation=unit.generation,
        desired_machines=1,
        max_machines=1,
        root_volume_gib=100,
        offer=ComputeOffer(
            id=unit.offer_id,
            provider=unit.provider_ref,
            instance_type="n3-A100-SXM4x8",
            region="US-1",
            gpu="A100-80",
            gpu_count=8,
            capacity_mode=ComputeCapacityMode.Pooled,
        ),
        bootstrap=ProviderUnitBootstrap(
            control_plane_url="https://control.example.com",
            enrollment_request_id=unit.id,
            agent_version="test",
            agent_sha256="a" * 64,
            agent_binary_url="https://control.example.com/agent",
        ),
    )
    nodes: list[Server] = []
    creates: list[_Creation] = []

    def respond(transport: httpx.HTTPTransport, incoming: httpx.Request) -> httpx.Response:
        del transport
        path = incoming.url.path
        if path == "/v1/core/environments":
            return httpx.Response(
                200, json={"status": True, "environments": [{"name": "test", "region": "US-1"}]}
            )
        if path == "/v1/core/images":
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "images": [
                        {
                            "images": [
                                {
                                    "id": 1,
                                    "name": "published-node",
                                    "region_name": "US-1",
                                    "is_public": False,
                                }
                            ]
                        }
                    ],
                },
            )
        if path == "/v1/core/virtual-machines":
            if incoming.method == "POST":
                creates.append(_Creation.model_validate_json(incoming.content))
                raise httpx.ReadTimeout("lost create response")
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "instances": [node.model_dump(mode="json") for node in nodes],
                    "count": len(nodes),
                    "page": 1,
                    "page_size": 100,
                },
            )
        if path == "/v1/core/virtual-machines/71" and incoming.method == "DELETE":
            nodes.clear()
            return httpx.Response(200, json={"status": True})
        if path == "/v1/core/virtual-machines/71" and incoming.method == "GET":
            if not nodes:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"status": True, "instance": nodes[0].model_dump(mode="json")}
            )
        raise AssertionError(f"unexpected provider request {incoming.method} {path}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    provider = HyperstackPooledProvider(
        provider_ref=unit.provider_ref,
        client=HyperstackClient(SecretStr("test")),
        deployments_by_region={
            "US-1": HyperstackDeployment(
                environment_name="test",
                keypair_name="test",
                image_name="published-node",
                recipe_sha256="a" * 64,
            )
        },
        launch_credentials=services.provider_node_launches,
    )
    with pytest.raises(HyperstackError, match="transport_unavailable"):
        provider.ensure_unit(request)
    with pytest.raises(HyperstackError, match="creation_outcome_pending"):
        provider.ensure_unit(request)
    assert len(creates) == 1
    assert provider.delete_unit(request).phase == ProviderCapacityPhase.Deleting
    created = creates[0]
    nodes.append(
        Server(
            id=71,
            name=created.name,
            labels=created.labels,
            created_at=datetime.now(UTC),
            status="BUILD",
            environment=Environment(name="test", region="US-1"),
            flavor=Flavor.model_validate(
                {
                    "name": "n3-A100-SXM4x8",
                    "cpu": 192,
                    "ram": 960,
                    "disk": 100,
                    "ephemeral": 16000,
                    "gpu": "A100-80G-SXM4",
                    "gpu_count": 8,
                }
            ),
            volume_attachments=(),
        )
    )
    owned = nodes[0]
    foreign_launch_id = str(uuid4())
    nodes[0] = owned.model_copy(
        update={
            "name": owned.name.rsplit("-", 1)[0] + "-" + foreign_launch_id.replace("-", ""),
            "labels": tuple(
                f"lazycloud-launch={foreign_launch_id}"
                if label.startswith("lazycloud-launch=")
                else label
                for label in owned.labels
            ),
        }
    )
    with pytest.raises(ValueError, match="no durable provider resource identity"):
        provider.release_machine(request, nodes[0].name)
    assert len(nodes) == 1
    nodes[0] = owned
    provider.describe_unit(request)
    nodes[0] = owned.model_copy(update={"name": "renamed-by-operator"})
    observed = provider.describe_unit(request)
    assert observed.instances[0].provider_instance_id == created.name
    assert not provider.machine_storage_destroyed(request, created.name, ())
    renamed = nodes[0]
    nodes[0] = renamed.model_copy(update={"labels": ()})
    with pytest.raises(ValueError, match="does not belong to the capacity unit"):
        provider.delete_unit(request)
    assert len(nodes) == 1
    nodes[0] = renamed
    assert provider.delete_unit(request).phase == ProviderCapacityPhase.Deleted
    assert provider.machine_storage_destroyed(request, created.name, ())
    assert not services.provider_node_launches.for_unit(request)
    assert len(creates) == 1
