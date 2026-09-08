from uuid import uuid4

import pytest
from api.server.services import ApiServices
from compute.offers import ComputeOffer
from compute.providers import ProviderCapacityPhase, ProviderUnitBootstrap, ProviderUnitRequest
from database.repositories.compute import ComputeUnitRepository
from provider_ovh.client import Instance, Operation, OvhClient, Volume
from provider_ovh.pooled_provider import OvhPooledProvider
from pydantic import SecretStr
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
)
from shared.errors import UpstreamUnavailableError


@pytest.mark.parametrize("operation_status", [None, "in-progress", "in-error"])
def test_pending_create_and_cleanup_recover_without_leaking_instances(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    operation_status: str | None,
) -> None:
    services = isolated_services
    unit_id = str(uuid4())
    flavor_id = str(uuid4())
    region = "US-EAST-VA-1"
    with services.context.database.session() as session:
        unit = ComputeUnitRepository(session).upsert(
            ComputeUnitRecord(
                id=unit_id,
                workspace_id=services.context.default_workspace_id(session),
                name=UnitName("ovh-cleanup"),
                pool=MachinePool("lazycloud"),
                capacity_owner_id=unit_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                provider="ovh",
                provider_ref="ovh:test",
                platform_fleet=True,
                capacity_mode=ComputeCapacityMode.Pooled,
                visibility=ComputeUnitVisibility.Internal,
                region=region,
                offer_id=f"{region}:{flavor_id}:b3-16",
                capability_key=f"ovh:{region}:b3-16:amd64:runsc",
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
        offer=ComputeOffer(
            id=unit.offer_id,
            provider=unit.provider_ref,
            instance_type="b3-16",
            region=region,
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
    slot = f"lc-{unit.id}-{unit.generation}-0"
    launch = services.provider_node_launches.prepare(request, slot)
    assert services.provider_node_launches.mark_creation_attempt(launch.launch_id)
    if operation_status is not None:
        services.provider_node_launches.record_creation_operation(launch.launch_id, "operation")

        def get_operation(client: OvhClient, operation_id: str) -> Operation:
            del client, operation_id
            return Operation(
                id="operation",
                status=operation_status,
                resourceId=None,
                subOperations=None,
            )

        monkeypatch.setattr(OvhClient, "operation", get_operation)
    instances: dict[str, Instance] = {}

    def list_instances(client: OvhClient, selected_region: str) -> tuple[Instance, ...]:
        del client, selected_region
        return tuple(instances.values())

    def get_instance(client: OvhClient, instance_id: str, selected_region: str) -> Instance | None:
        del client, selected_region
        return instances.get(instance_id)

    def delete_instance(client: OvhClient, instance_id: str) -> None:
        del client
        del instances[instance_id]

    def volumes(client: OvhClient) -> tuple[Volume, ...]:
        del client
        return ()

    monkeypatch.setattr(OvhClient, "instances", list_instances)
    monkeypatch.setattr(OvhClient, "instance", get_instance)
    monkeypatch.setattr(OvhClient, "delete_instance", delete_instance)
    monkeypatch.setattr(OvhClient, "volumes", volumes)
    provider = OvhPooledProvider(
        provider_ref=unit.provider_ref,
        client=OvhClient(SecretStr("test"), SecretStr("test"), SecretStr("test"), "test"),
        images_by_region={},
        launch_credentials=services.provider_node_launches,
    )
    if operation_status == "in-error":
        assert provider.delete_unit(request).phase == ProviderCapacityPhase.Deleted
        assert services.provider_node_launches.for_server(request, slot) is None
        return
    if operation_status is None:
        with pytest.raises(UpstreamUnavailableError, match="create outcome is unresolved"):
            provider.ensure_unit(request)
    else:
        assert provider.ensure_unit(request).phase == ProviderCapacityPhase.Provisioning
    assert provider.delete_unit(request).phase == ProviderCapacityPhase.Deleting

    late = Instance(
        id=str(uuid4()),
        name=f"{slot}-{'a' * 64}-{launch.launch_id}",
        region=region,
        flavorId=flavor_id,
        imageId=str(uuid4()),
        status="ACTIVE",
        addresses=(),
        attachedVolumes=(),
    )
    unrelated = late.model_copy(update={"id": str(uuid4()), "name": "customer-managed"})
    instances.update({late.id: late, unrelated.id: unrelated})
    snapshot = provider.delete_unit(request)
    assert snapshot.phase == ProviderCapacityPhase.Deleted
    assert tuple(instances) == (unrelated.id,)
    status = services.provider_node_launches.for_server(request, slot)
    assert status is not None and status.provider_instance_id == late.id
