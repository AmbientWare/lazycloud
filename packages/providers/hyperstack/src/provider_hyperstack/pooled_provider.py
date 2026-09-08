from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256

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
from shared.gpu import normalize_gpu_type
from shared.provider_config import ProviderKind
from shared.supplier_costs import SupplierCostTerms, SupplierCpuUnit, SupplierNetworkTerms
from shared.timestamps import to_utc, utc_now

from provider_hyperstack.capacity_policy import GPU_MODELS, HYPERSTACK_CAPACITY_POLICY
from provider_hyperstack.client import HyperstackClient, HyperstackError, Server
from provider_hyperstack.identity import labels, provider_label


class HyperstackDeployment(BaseModel):
    model_config = ConfigDict(frozen=True)

    environment_name: str = Field(min_length=1)
    keypair_name: str = Field(min_length=1)
    image_name: str = Field(min_length=1)
    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class HyperstackPooledProvider:
    provider_ref: str
    client: HyperstackClient
    deployments_by_region: Mapping[str, HyperstackDeployment]
    launch_credentials: ProviderNodeLaunchCredentials

    def unit_offer(self, unit: ComputeUnitRecord) -> ComputeOffer:
        region, separator, shape = unit.offer_id.partition(":")
        if (
            unit.provider_ref != self.provider_ref
            or region != unit.region
            or not separator
            or not shape
        ):
            raise ValueError("Hyperstack unit has invalid provider offer identity")
        return recorded_unit_offer(unit, cloud="hyperstack", instance_type=shape)

    def list_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        now = utc_now()
        rates: dict[str, Decimal] = {}
        for price in self.client.prices():
            if price.start_time is not None and to_utc(price.start_time) > now:
                continue
            if price.end_time is not None and to_utc(price.end_time) <= now:
                continue
            if price.name in rates:
                raise ValueError("Hyperstack returned overlapping pricebook entries")
            rates[price.name] = price.value
        for region in HYPERSTACK_CAPACITY_POLICY.allowed_regions:
            if region not in self.deployments_by_region:
                continue
            for shape in self.client.flavors(region):
                if (
                    shape.name not in HYPERSTACK_CAPACITY_POLICY.allowed_instance_types
                    or shape.region_name != region
                    or not shape.stock_available
                    or shape.disk < root_volume_gib
                    or shape.gpu_count <= 0
                ):
                    continue
                if (
                    shape.gpu_count != 8
                    or normalize_gpu_type(shape.gpu) != GPU_MODELS[shape.name].value
                ):
                    raise ValueError(
                        "Hyperstack flavor no longer matches its approved GPU configuration"
                    )
                # GPU flavors currently price CPU, RAM and local disks at zero.
                # Read those terms too so a supplier change cannot hide added costs.
                compute = (
                    rates[shape.gpu] * shape.gpu_count
                    + rates["vCPU"] * shape.cpu
                    + rates["RAM"] * shape.ram
                )
                disk = rates["hypervisor-local-storage"] * (shape.disk + shape.ephemeral)
                if compute <= 0:
                    raise ValueError("Hyperstack returned a nonpositive GPU price")
                yield pooled_cloud_offer(
                    offer_id=f"{region}:{shape.name}",
                    provider=self.provider_ref,
                    cloud="hyperstack",
                    instance_type=shape.name,
                    region=region,
                    cpu_millicores=shape.cpu * 1000,
                    memory_mb=int(shape.ram * 1024),
                    storage_mb=shape.disk * 1024,
                    gpu=GPU_MODELS[shape.name].value,
                    gpu_count=shape.gpu_count,
                    supplier_cpu_unit=SupplierCpuUnit.PhysicalCore,
                    supplier_cpu_count=shape.cpu,
                    capability_key=f"hyperstack:{region}:{shape.name}:amd64:runsc",
                    cost_terms=SupplierCostTerms(
                        source="api:hyperstack.pricebook;docs:hyperstack.billing-policies",
                        observed_at=now,
                        compute_hourly_micros=_micros(compute),
                        root_disk_hourly_micros=_micros(disk),
                        public_ipv4_hourly_micros=_micros(rates["PublicIP"]),
                        setup_micros=0,
                        billing_minimum_seconds=60,
                        billing_quantum_seconds=60,
                        network=SupplierNetworkTerms(
                            ingress_micros_per_gb=0, egress_micros_per_gb=0
                        ),
                    ),
                )

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self.set_unit_capacity(
            request, desired_machines=request.desired_machines, max_machines=request.max_machines
        )

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        servers = self._servers(request)
        for server in servers:
            self._bind(request, server)
        return self._snapshot(request, servers)

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot:
        if not 0 <= desired_machines <= max_machines:
            raise ValueError("invalid Hyperstack capacity bounds")
        request = request.model_copy(
            update={"desired_machines": desired_machines, "max_machines": max_machines}
        )
        servers = self._servers(request)
        for server in servers:
            self._bind(request, server)
        if len(servers) >= desired_machines:
            return self._snapshot(request, servers)
        if not HYPERSTACK_CAPACITY_POLICY.accepts(request.offer):
            raise ValueError("Hyperstack offer is not approved for new capacity")
        if self._unresolved_launches(request):
            raise HyperstackError("creation_outcome_pending", 0)
        deployment = self.deployments_by_region[request.offer.region]
        self._validate_deployment(request, deployment)
        occupied = {labels(server.labels)["lazycloud-slot"] for server in servers}
        for index in range(max_machines):
            if len(servers) >= desired_machines:
                break
            slot = f"{_prefix(request)}{request.generation}-{index}"
            if slot in occupied:
                continue
            prior = self.launch_credentials.for_server(request, slot)
            if prior is not None and prior.creation_attempted:
                if prior.provider_instance_id is None:
                    raise HyperstackError("creation_outcome_pending", 0)
                if self._server_for_instance(request, prior.provider_instance_id) is not None:
                    raise HyperstackError("creation_outcome_pending", 0)
                self.launch_credentials.revoke(prior.launch_id)
            launch = self.launch_credentials.prepare(request, slot)
            name = f"{slot}-{launch.launch_id.replace('-', '')}"
            metadata = {
                "lazycloud-managed": "true",
                "lazycloud-unit": request.unit_id,
                "lazycloud-provider": provider_label(self.provider_ref),
                "lazycloud-generation": str(request.generation),
                "lazycloud-launch": launch.launch_id,
                "lazycloud-slot": slot,
                "lazycloud-release": deployment.recipe_sha256,
            }
            body: dict[str, JsonValue] = {
                "name": name,
                "environment_name": deployment.environment_name,
                "flavor_name": request.offer.instance_type,
                "image_name": deployment.image_name,
                "key_name": deployment.keypair_name,
                "count": 1,
                "assign_floating_ip": True,
                "enable_port_randomization": False,
                "create_bootable_volume": False,
                "security_rules": [],
                "labels": [f"{key}={value}" for key, value in metadata.items()],
                "user_data": bootstrap_script(request, launch, name),
            }
            if not self.launch_credentials.mark_creation_attempt(launch.launch_id):
                raise HyperstackError("creation_outcome_pending", 0)
            try:
                server = self.client.create_server(body)
            except HyperstackError as exc:
                if exc.status_code in {400, 401, 403, 404, 405, 422}:
                    self.launch_credentials.revoke(launch.launch_id)
                    raise
                # Once submitted, an absent list result cannot prove no VM was created.
                # The durable launch fence prevents a second purchase on retry.
                recovered = self.client.server(name)
                if recovered is None:
                    raise
                server = recovered
            self._validate_server(request, server)
            self._bind(request, server)
            servers.append(server)
        return self._snapshot(request, servers)

    def release_machine(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        server = self._server_for_instance(request, provider_instance_id)
        if server is not None:
            self._validate_server(request, server)
            self._bind(request, server)
            self.client.delete_server(server.id)
        return self.describe_unit(request)

    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        for server in self._servers(request):
            self._bind(request, server)
            self.client.delete_server(server.id)
        request = request.model_copy(update={"desired_machines": 0})
        servers = self._servers(request)
        unresolved = False
        if not servers:
            for launch in self.launch_credentials.for_unit(request):
                if launch.creation_attempted and launch.provider_instance_id is None:
                    unresolved = True
                else:
                    self.launch_credentials.revoke(launch.launch_id)
        return self._snapshot(request, servers).model_copy(
            update={
                "phase": ProviderCapacityPhase.Deleting
                if servers or unresolved
                else ProviderCapacityPhase.Deleted
            }
        )

    def machine_storage_destroyed(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        if storage_volume_ids:
            raise ValueError("Hyperstack managed nodes cannot own persistent volumes")
        server = self._server_for_instance(request, provider_instance_id)
        if server is not None:
            self._validate_server(request, server)
            return False
        return True

    def _servers(self, request: ProviderUnitRequest) -> list[Server]:
        servers = {
            server.id: server
            for server in self.client.servers(search=_prefix(request))
            if server.name.startswith(_prefix(request))
        }
        for launch in self.launch_credentials.for_unit(request):
            if launch.provider_instance_id is None:
                continue
            server = self._server_for_instance(request, launch.provider_instance_id)
            if server is not None:
                servers[server.id] = server
        for server in servers.values():
            self._validate_server(request, server)
        return list(servers.values())

    def _server_for_instance(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> Server | None:
        launch = self.launch_credentials.for_instance(request, provider_instance_id)
        if (
            launch is None
            or not launch.provider_resource_id
            or not launch.provider_resource_id.isdecimal()
            or int(launch.provider_resource_id) <= 0
        ):
            raise ValueError("Hyperstack instance has no durable provider resource identity")
        return self.client.server_by_id(int(launch.provider_resource_id))

    def _validate_server(self, request: ProviderUnitRequest, server: Server) -> None:
        metadata = labels(server.labels)
        launch_id = metadata.get("lazycloud-launch", "")
        slot = metadata.get("lazycloud-slot", "")
        if (
            metadata.get("lazycloud-managed") != "true"
            or metadata.get("lazycloud-unit") != request.unit_id
            or metadata.get("lazycloud-provider") != provider_label(self.provider_ref)
            or not launch_id
            or not slot.startswith(_prefix(request))
            or not metadata.get("lazycloud-generation", "").isdecimal()
            or server.environment.region != request.offer.region
            or server.flavor.name != request.offer.instance_type
        ):
            raise ValueError("Hyperstack server does not belong to the capacity unit")
        if server.volume_attachments:
            raise ValueError("Hyperstack managed nodes cannot contain attached volumes")
        launch = self.launch_credentials.for_server(request, slot)
        if (
            request.provider_ref != self.provider_ref
            or launch is None
            or launch.revoked
            or not launch.creation_attempted
            or launch.launch_id != launch_id
            or launch.generation != int(metadata["lazycloud-generation"])
            or launch.provider_instance_id not in {None, _instance_identity(server)}
            or launch.provider_resource_id not in {None, str(server.id)}
            or (launch.provider_resource_id is None and server.name != _instance_identity(server))
        ):
            raise ValueError("Hyperstack server does not match an authorized launch")

    def _bind(self, request: ProviderUnitRequest, server: Server) -> None:
        metadata = labels(server.labels)
        self.launch_credentials.bind(
            metadata["lazycloud-launch"],
            provider_ref=self.provider_ref,
            provider_instance_id=_instance_identity(server),
            provider_resource_id=str(server.id),
            unit_id=request.unit_id,
            server_name=metadata["lazycloud-slot"],
            region=server.environment.region,
            generation=int(metadata["lazycloud-generation"]),
        )

    def _validate_deployment(
        self, request: ProviderUnitRequest, deployment: HyperstackDeployment
    ) -> None:
        if not any(
            environment.name == deployment.environment_name
            and environment.region == request.offer.region
            for environment in self.client.environments()
        ):
            raise ValueError("Hyperstack deployment environment does not match the offer region")
        images = [
            image
            for image in self.client.images(
                region=request.offer.region, search=deployment.image_name
            )
            if image.name == deployment.image_name
            and image.region_name == request.offer.region
            and not image.is_public
        ]
        if len(images) != 1:
            raise ValueError(
                "Hyperstack requires one published private node image in the offer region"
            )

    def _snapshot(
        self, request: ProviderUnitRequest, servers: list[Server]
    ) -> ProviderUnitSnapshot:
        deployment = self.deployments_by_region.get(request.offer.region)
        return ProviderUnitSnapshot(
            phase=ProviderCapacityPhase.Ready
            if len(servers) == request.desired_machines
            and all(server.status == "ACTIVE" for server in servers)
            and not self._unresolved_launches(request)
            else ProviderCapacityPhase.Provisioning,
            resource_id=request.unit_id,
            desired_machines=request.desired_machines,
            max_machines=request.max_machines,
            observed_machines=len(servers),
            instances=[
                ProviderUnitInstance(
                    provider_instance_id=_instance_identity(server),
                    status=_status(server.status),
                    address=server.floating_ip or "",
                    availability_zone=server.environment.region,
                    booted_template_version=labels(server.labels).get("lazycloud-release", ""),
                    billing_started_at=server.created_at,
                    billing_minimum_seconds=60,
                    billing_quantum_seconds=60,
                )
                for server in servers
            ],
            provider_state=ComputeUnitProviderState(resource_id=request.unit_id),
            current_template_version=deployment.recipe_sha256 if deployment is not None else "",
        )

    def _unresolved_launches(self, request: ProviderUnitRequest) -> bool:
        return any(
            launch.creation_attempted and launch.provider_instance_id is None
            for launch in self.launch_credentials.for_unit(request)
        )


def _prefix(request: ProviderUnitRequest) -> str:
    return f"lc-{sha256(request.unit_id.encode()).hexdigest()[:12]}-"


def _instance_identity(server: Server) -> str:
    metadata = labels(server.labels)
    return f"{metadata['lazycloud-slot']}-{metadata['lazycloud-launch'].replace('-', '')}"


def _micros(value: Decimal) -> int:
    return int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _status(status: str) -> str:
    if status == "ACTIVE":
        return ProviderMachineStatus.Active
    if status in {"BUILD", "CREATING", "STARTING"}:
        return ProviderMachineStatus.Pending
    if status in {"DELETING", "ERROR", "SHUTOFF", "HIBERNATED"}:
        return ProviderMachineStatus.Unhealthy
    return ProviderMachineStatus.Unknown


def bootstrap_script(
    request: ProviderUnitRequest, launch: ProviderNodeLaunchCredential, name: str
) -> str:
    return provider_bootstrap_script(
        NodeBootstrapSettings(
            control_plane_url=request.bootstrap.control_plane_url,
            enrollment_request_id=request.bootstrap.enrollment_request_id,
            agent_binary_url=request.bootstrap.agent_binary_url,
            agent_sha256=request.bootstrap.agent_sha256,
            gpu_count=request.offer.gpu_count,
        ),
        provider=ProviderKind.Hyperstack,
        region=request.offer.region,
        launch_id=launch.launch_id,
        bootstrap_token=launch.bootstrap_token,
        instance_identity_shell=(
            "resolve_provider_instance_id() { printf '%s' __INSTANCE_NAME__; }\n"
        ),
        values={"__INSTANCE_NAME__": name},
    )
