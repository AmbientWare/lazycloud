from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from uuid import UUID

from compute.node_bootstrap import (
    NodeBootstrapSettings,
    provider_bootstrap_script,
)
from compute.offers import ComputeOffer, pooled_cloud_offer, recorded_unit_offer
from compute.provider_launches import ProviderNodeLaunchCredential, ProviderNodeLaunchCredentials
from compute.providers import (
    ProviderCapacityPhase,
    ProviderMachineStatus,
    ProviderUnitInstance,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from shared.compute_policy import ComputeUnitProviderState, ComputeUnitRecord
from shared.errors import UpstreamUnavailableError
from shared.provider_config import ProviderKind
from shared.supplier_costs import SupplierCostTerms, SupplierCpuUnit, SupplierNetworkTerms
from shared.timestamps import utc_now

from provider_ovh.capacity_policy import OVH_CAPACITY_POLICY
from provider_ovh.client import Instance, OvhClient, OvhError


class OvhNodeImage(BaseModel):
    model_config = ConfigDict(frozen=True)

    image_id: UUID
    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class OvhPooledProvider:
    provider_ref: str
    client: OvhClient
    images_by_region: Mapping[str, OvhNodeImage]
    launch_credentials: ProviderNodeLaunchCredentials

    def unit_offer(self, unit: ComputeUnitRecord) -> ComputeOffer:
        region, _, shape = _offer_identity(unit.offer_id)
        if unit.provider_ref != self.provider_ref or region != unit.region:
            raise ValueError("OVHcloud unit has invalid provider offer identity")
        return recorded_unit_offer(unit, cloud="ovh", instance_type=shape)

    def list_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        catalog = self.client.catalog()
        disk_price = catalog.plan("instance.local-storage-gen3-gb.hour.consumption")
        ip_price = catalog.plan("publicip.ip.hour.consumption").hourly_micros()
        for region in OVH_CAPACITY_POLICY.allowed_regions:
            if region not in self.images_by_region:
                continue
            for flavor in self.client.flavors(region):
                if (
                    flavor.name not in OVH_CAPACITY_POLICY.allowed_instance_types
                    or flavor.region != region
                    or flavor.os_type != "linux"
                    or not flavor.available
                    or flavor.quota == 0
                    or flavor.disk < root_volume_gib
                    or not flavor.plan_codes.hourly
                ):
                    continue
                price = catalog.plan(flavor.plan_codes.hourly).hourly_micros()
                if price <= 0:
                    raise ValueError("OVHcloud compute price must be positive")
                offer = pooled_cloud_offer(
                    offer_id=f"{region}:{flavor.id}:{flavor.name}",
                    provider=self.provider_ref,
                    cloud="ovh",
                    instance_type=flavor.name,
                    region=region,
                    cpu_millicores=flavor.vcpus * 1000,
                    memory_mb=flavor.ram * 1024,
                    storage_mb=flavor.disk * 1024,
                    cost_terms=SupplierCostTerms(
                        source="api:ovh.us.cloud.catalog;flavor.planCodes.hourly;local-storage;publicip",
                        observed_at=utc_now(),
                        compute_hourly_micros=price,
                        root_disk_hourly_micros=disk_price.hourly_micros(quantity=flavor.disk),
                        public_ipv4_hourly_micros=ip_price,
                        setup_micros=0,
                        billing_minimum_seconds=0,
                        billing_quantum_seconds=1,
                        network=SupplierNetworkTerms(
                            ingress_micros_per_gb=0, egress_micros_per_gb=0
                        ),
                    ),
                    supplier_cpu_unit=SupplierCpuUnit.Vcpu,
                    supplier_cpu_count=flavor.vcpus,
                    capability_key=f"ovh:{region}:{flavor.name}:amd64:runsc",
                )
                yield offer.model_copy(update={"available": flavor.quota})

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self.set_unit_capacity(
            request,
            desired_machines=request.desired_machines,
            max_machines=request.max_machines,
        )

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self._snapshot(request, self._servers(request))

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot:
        if not 0 <= desired_machines <= max_machines:
            raise ValueError("invalid OVHcloud capacity bounds")
        request = request.model_copy(
            update={
                "desired_machines": desired_machines,
                "max_machines": max_machines,
            }
        )
        self._reconcile_creations(request)
        servers = self._servers(request)
        for server in servers:
            self._bind_server(request, server)
        if len(servers) >= desired_machines:
            return self._snapshot(request, servers)
        pending = [
            launch
            for launch in self.launch_credentials.for_unit(request)
            if launch.creation_attempted and launch.provider_instance_id is None
        ]
        if any(launch.provider_operation_id is None for launch in pending):
            raise UpstreamUnavailableError(
                "OVHcloud create outcome is unresolved; replacement is blocked"
            )
        if len(servers) + len(pending) >= desired_machines:
            return self._snapshot(request, servers)
        if not OVH_CAPACITY_POLICY.accepts(request.offer):
            raise ValueError("OVHcloud offer is not approved for new capacity")
        image = self.images_by_region[request.offer.region]
        observed_image = self.client.image(str(image.image_id))
        if (
            observed_image.status != "active"
            or observed_image.region != request.offer.region
            or re.fullmatch(
                rf"lazycloud-node-{image.recipe_sha256}-[0-9a-f-]{{36}}",
                observed_image.name,
            )
            is None
            or observed_image.min_disk * 1024 > request.offer.storage_mb
            or observed_image.min_ram > request.offer.memory_mb
        ):
            raise ValueError("OVHcloud node image does not match the published release or flavor")
        _, flavor_id, _ = _offer_identity(request.offer.id)
        occupied = {self._slot(request, server)[0] for server in servers}
        for index in range(max_machines):
            if len(servers) >= desired_machines:
                break
            slot = f"lc-{request.unit_id}-{request.generation}-{index}"
            if slot in occupied:
                continue
            prior = self.launch_credentials.for_server(request, slot)
            if prior is not None and prior.creation_attempted:
                if prior.provider_instance_id is None:
                    raise UpstreamUnavailableError(
                        "OVHcloud create outcome is unresolved; replacement is blocked"
                    )
                if (
                    self.client.instance(prior.provider_instance_id, request.offer.region)
                    is not None
                ):
                    raise UpstreamUnavailableError(
                        "OVHcloud launched instance is missing from unit inventory"
                    )
                self.launch_credentials.revoke(prior.launch_id)
            elif prior is not None and (prior.redeemed or prior.expires_at <= utc_now()):
                self.launch_credentials.revoke(prior.launch_id)
            launch = self.launch_credentials.prepare(request, slot)
            body: dict[str, JsonValue] = {
                "name": f"{slot}-{image.recipe_sha256}-{launch.launch_id}",
                "flavor": {"id": flavor_id},
                "bootFrom": {"imageId": str(image.image_id)},
                "billingPeriod": "hourly",
                "network": {"public": True},
                "bulk": 1,
                "userData": bootstrap_script(request, launch),
            }
            if not self.launch_credentials.mark_creation_attempt(launch.launch_id):
                raise UpstreamUnavailableError("OVHcloud instance creation is already in progress")
            try:
                operation = self.client.create_instance(request.offer.region, body)
            except OvhError as exc:
                if exc.status_code in {400, 401, 403, 404, 422, 429}:
                    self.launch_credentials.revoke(launch.launch_id)
                else:
                    recovered = self._servers(request)
                    if any(self._slot(request, item)[0] == slot for item in recovered):
                        for item in recovered:
                            self._bind_server(request, item)
                        servers = recovered
                        continue
                raise
            self.launch_credentials.record_creation_operation(launch.launch_id, operation.id)
            self._reconcile_creations(request)
            return self._snapshot(request, self._servers(request))
        return self._snapshot(request, servers)

    def release_machine(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderUnitSnapshot:
        server = self.client.instance(provider_instance_id, request.offer.region)
        if server is not None:
            self._validate_server(request, server)
            self._bind_server(request, server)
            self._require_local_storage((server,))
            self.client.delete_instance(server.id)
        return self.describe_unit(request)

    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        self._reconcile_creations(request)
        servers = self._servers(request)
        self._require_local_storage(servers)
        for server in servers:
            self._bind_server(request, server)
            self.client.delete_instance(server.id)
        request = request.model_copy(update={"desired_machines": 0})
        remaining = self._servers(request)
        unresolved = any(
            launch.creation_attempted and launch.provider_instance_id is None
            for launch in self.launch_credentials.for_unit(request)
        )
        return self._snapshot(request, remaining).model_copy(
            update={
                "phase": ProviderCapacityPhase.Deleting
                if remaining or unresolved
                else ProviderCapacityPhase.Deleted,
            }
        )

    def machine_storage_destroyed(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        if self.client.instance(provider_instance_id, request.offer.region) is not None:
            return False
        volumes = self.client.volumes()
        return not any(
            volume.id in storage_volume_ids or provider_instance_id in volume.attached_to
            for volume in volumes
        )

    def _reconcile_creations(self, request: ProviderUnitRequest) -> None:
        for server in self._servers(request):
            self._bind_server(request, server)
        for launch in self.launch_credentials.for_unit(request):
            if launch.provider_instance_id is not None or launch.provider_operation_id is None:
                continue
            operation = self.client.operation(launch.provider_operation_id)
            resources = {
                resource_id
                for resource_id in (
                    operation.resource_id,
                    *(child.resource_id for child in operation.sub_operations or ()),
                )
                if resource_id
            }
            observed = False
            for resource_id in resources:
                server = self.client.instance(str(UUID(resource_id)), request.offer.region)
                if server is not None:
                    self._validate_server(request, server)
                    self._bind_server(request, server)
                    observed = True
            if (
                not observed
                and not resources
                and operation.status in {"canceled", "in-error"}
                and all(
                    child.status in {"canceled", "in-error"}
                    for child in operation.sub_operations or ()
                )
            ):
                self.launch_credentials.revoke(launch.launch_id)

    def _servers(self, request: ProviderUnitRequest) -> list[Instance]:
        servers = [
            server
            for server in self.client.instances(request.offer.region)
            if server.name.startswith(f"lc-{request.unit_id}-")
        ]
        observed_ids = {server.id for server in servers}
        for launch in self.launch_credentials.for_unit(request):
            if launch.provider_instance_id is None or launch.provider_instance_id in observed_ids:
                continue
            server = self.client.instance(launch.provider_instance_id, request.offer.region)
            if server is not None:
                servers.append(server)
                observed_ids.add(server.id)
        for server in servers:
            self._validate_server(request, server)
        return servers

    def _slot(self, request: ProviderUnitRequest, server: Instance) -> tuple[str, int, str, str]:
        match = re.fullmatch(
            rf"(lc-{re.escape(request.unit_id)}-([1-9][0-9]*)-[0-9]+)-([0-9a-f]{{64}})-([0-9a-f-]{{36}})",
            server.name,
        )
        if match is None:
            raise ValueError("OVHcloud instance has no valid managed launch identity")
        slot, generation, release, launch_id = match.groups()
        return slot, int(generation), release, str(UUID(launch_id))

    def _validate_server(self, request: ProviderUnitRequest, server: Instance) -> None:
        _, flavor_id, _ = _offer_identity(request.offer.id)
        slot, _, _, launch_id = self._slot(request, server)
        launch = self.launch_credentials.for_server(request, slot)
        if (
            request.provider_ref != self.provider_ref
            or launch is None
            or launch.launch_id != launch_id
            or launch.revoked
            or launch.provider_instance_id not in {None, server.id}
            or server.region != request.offer.region
            or server.flavor_id != flavor_id
        ):
            raise ValueError("OVHcloud instance does not belong to the requested capacity unit")

    def _bind_server(self, request: ProviderUnitRequest, server: Instance) -> None:
        slot, generation, _, launch_id = self._slot(request, server)
        self.launch_credentials.bind(
            launch_id,
            provider_ref=self.provider_ref,
            provider_instance_id=server.id,
            unit_id=request.unit_id,
            server_name=slot,
            region=server.region,
            generation=generation,
        )

    def _require_local_storage(self, servers: Iterable[Instance]) -> None:
        servers = tuple(servers)
        if any(server.attached_volumes for server in servers):
            raise ValueError("OVHcloud managed instance has unexpected persistent volumes")
        ids = {server.id for server in servers}
        if ids and any(ids.intersection(volume.attached_to) for volume in self.client.volumes()):
            raise ValueError(
                "OVHcloud managed instances have unexpected persistent volumes; deletion refused"
            )

    def _snapshot(
        self, request: ProviderUnitRequest, servers: list[Instance]
    ) -> ProviderUnitSnapshot:
        image = self.images_by_region.get(request.offer.region)
        pending = any(
            launch.creation_attempted and launch.provider_instance_id is None
            for launch in self.launch_credentials.for_unit(request)
        )
        return ProviderUnitSnapshot(
            phase=ProviderCapacityPhase.Ready
            if len(servers) == request.desired_machines
            and not pending
            and all(server.status == "ACTIVE" for server in servers)
            else ProviderCapacityPhase.Provisioning,
            resource_id=request.unit_id,
            desired_machines=request.desired_machines,
            max_machines=request.max_machines,
            observed_machines=len(servers),
            instances=[
                ProviderUnitInstance(
                    provider_instance_id=server.id,
                    status=_status(server.status),
                    address=next(
                        (
                            ip.ip
                            for ip in server.ip_addresses
                            if ip.version == 4 and ip.type == "public"
                        ),
                        "",
                    ),
                    availability_zone=server.region,
                    booted_template_version=self._slot(request, server)[2],
                    billing_minimum_seconds=0,
                    billing_quantum_seconds=1,
                )
                for server in servers
            ],
            provider_state=ComputeUnitProviderState(resource_id=request.unit_id),
            current_template_version=image.recipe_sha256 if image is not None else "",
        )


def _offer_identity(offer_id: str) -> tuple[str, str, str]:
    region, flavor_id, shape = offer_id.split(":", 2)
    return region, str(UUID(flavor_id)), shape


def _status(status: str) -> str:
    if status == "ACTIVE":
        return ProviderMachineStatus.Active
    if status in {"BUILD", "BUILDING", "REBUILD"}:
        return ProviderMachineStatus.Pending
    if status in {"DELETED", "SOFT_DELETED"}:
        return ProviderMachineStatus.Terminated
    return ProviderMachineStatus.Unhealthy


def bootstrap_script(request: ProviderUnitRequest, launch: ProviderNodeLaunchCredential) -> str:
    return provider_bootstrap_script(
        NodeBootstrapSettings(
            control_plane_url=request.bootstrap.control_plane_url,
            enrollment_request_id=request.bootstrap.enrollment_request_id,
            agent_binary_url=request.bootstrap.agent_binary_url,
            agent_sha256=request.bootstrap.agent_sha256,
        ),
        provider=ProviderKind.Ovh,
        region=request.offer.region,
        launch_id=launch.launch_id,
        bootstrap_token=launch.bootstrap_token,
        instance_identity_shell=_IDENTITY_SHELL,
        values={},
    )


_IDENTITY_SHELL = r"""
resolve_provider_instance_id() {
  curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 \
    http://169.254.169.254/openstack/latest/meta_data.json \
    | python3 -c 'import json,sys,uuid; print(uuid.UUID(json.load(sys.stdin)["uuid"]))'
}
"""
