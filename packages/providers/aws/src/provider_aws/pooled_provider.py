from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from compute.offers import (
    DEFAULT_POOLED_NODE_ARCHITECTURE,
    DEFAULT_POOLED_NODE_RUNTIME,
    ComputeOffer,
    pooled_cloud_offer,
    recorded_unit_offer,
)
from compute.providers import (
    PooledCapacityProvider,
    ProviderCapacityPhase,
    ProviderMachineStatus,
    ProviderUnitInstance,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
)
from pydantic import ValidationError
from shared.aws_connections import AwsAccountNetwork
from shared.compute_policy import (
    ComputeUnitProviderState,
    ComputeUnitRecord,
)
from shared.network_egress import NetworkEgressRouteEvidence
from shared.supplier_costs import SupplierCostTerms, SupplierCpuUnit

from .account_connection import AwsAccountConnectionTarget
from .instance_catalog import (
    AWS_INSTANCE_CATALOG,
    AwsInstanceCatalogEntry,
    AwsInstanceCategory,
    aws_instance_catalog_entry,
)
from .managed_pool import (
    AwsManagedPoolBinaries,
    AwsManagedPoolBootstrap,
    AwsManagedPoolClientProvider,
    AwsManagedPoolInstanceDetails,
    AwsManagedPoolPhase,
    AwsManagedPoolProvisioner,
    AwsManagedPoolResourceIds,
    AwsManagedPoolSnapshot,
    AwsManagedPoolSpec,
)
from .network_egress import same_region_storage_destinations
from .spot_prices import load_aws_spot_quotes
from .supplier_prices import AwsRegionalPrices

_PHASES = {
    AwsManagedPoolPhase.Provisioning: ProviderCapacityPhase.Provisioning,
    AwsManagedPoolPhase.Ready: ProviderCapacityPhase.Ready,
    AwsManagedPoolPhase.Deleting: ProviderCapacityPhase.Deleting,
    AwsManagedPoolPhase.Deleted: ProviderCapacityPhase.Deleted,
}


@dataclass(frozen=True, slots=True)
class AwsConnectedAccountPooledProvider(PooledCapacityProvider):
    provider_ref: str
    connection: AwsAccountConnectionTarget
    networks: Mapping[str, AwsAccountNetwork]
    binaries_by_region: Mapping[str, AwsManagedPoolBinaries]
    client_provider: AwsManagedPoolClientProvider
    regional_prices: Mapping[str, AwsRegionalPrices] = field(
        default_factory=lambda: dict[str, AwsRegionalPrices]()
    )

    def unbilled_network_destinations(
        self, unit: ComputeUnitRecord, provider_instance_id: str
    ) -> NetworkEgressRouteEvidence:
        network = self._network(unit.region)
        clients = self.client_provider.assume(self._target(unit.region))
        return same_region_storage_destinations(
            clients.ec2,
            region=unit.region,
            instance_id=provider_instance_id,
            vpc_id=network.vpc_id,
            subnet_ids=network.subnet_ids,
        )

    def unit_offer(self, unit: ComputeUnitRecord) -> ComputeOffer:
        parts = unit.offer_id.split(":")
        region = parts[0]
        instance_type = parts[1] if len(parts) >= 2 else ""
        expected = f"{region}:{instance_type}"
        if unit.worker_preemptible:
            expected += f":spot:{unit.offer_availability_zone}"
        elif unit.offer_availability_zone:
            expected += f":{unit.offer_availability_zone}"
        if (
            unit.provider_ref != self.provider_ref
            or region != unit.region
            or not instance_type
            or unit.offer_id != expected
        ):
            raise ValueError("AWS unit has invalid provider offer identity")
        return recorded_unit_offer(unit, cloud="aws", instance_type=instance_type)

    def list_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        offers: list[ComputeOffer] = []
        for region, artifacts in sorted(self.binaries_by_region.items()):
            if region not in self.networks:
                continue
            regional_prices = self.regional_prices.get(region)
            instances: list[AwsInstanceCatalogEntry] = []
            on_demand: list[ComputeOffer] = []
            for instance in AWS_INSTANCE_CATALOG:
                ami_id = (
                    artifacts.cpu_ami_id
                    if instance.kind is AwsInstanceCategory.Cpu
                    else artifacts.gpu_ami_id
                )
                if ami_id is None:
                    continue
                instances.append(instance)
                compute_price = (
                    regional_prices.instance_hourly_micros.get(instance.instance_type)
                    if regional_prices is not None
                    else None
                )
                if compute_price is not None and False in instance.purchase_markets:
                    on_demand.append(
                        self._offer(
                            instance,
                            region=region,
                            root_volume_gib=root_volume_gib,
                            cost_terms=SupplierCostTerms(
                                source="code:provider_aws.supplier_prices;reviewed:2026-09-10;gp3:30-day-month",
                                compute_hourly_micros=compute_price,
                            ),
                            regional_prices=regional_prices,
                        )
                    )
            network = self._network(region)
            clients = self.client_provider.assume(self._target(region))
            market = load_aws_spot_quotes(
                clients.ec2,
                network=network,
                instance_types=tuple(
                    instance.instance_type
                    for instance in instances
                    if True in instance.purchase_markets
                ),
            )
            offers.extend(on_demand)
            for offer in on_demand:
                for zone in market.availability_zones:
                    offers.append(
                        offer.model_copy(
                            update={
                                "id": f"{offer.id}:{zone}",
                                "availability_zone": zone,
                                "capability_key": f"{offer.capability_key}:{zone}",
                            }
                        )
                    )
            for quote in market.quotes:
                instance = aws_instance_catalog_entry(quote.instance_type)
                offers.append(
                    self._offer(
                        instance,
                        region=region,
                        root_volume_gib=root_volume_gib,
                        preemptible=True,
                        availability_zone=quote.availability_zone,
                        cost_terms=SupplierCostTerms(
                            source="aws:DescribeSpotPriceHistory;code:provider_aws.supplier_prices;gp3:30-day-month",
                            observed_at=quote.observed_at,
                            effective_at=quote.effective_at,
                            compute_hourly_micros=quote.compute_hourly_micros,
                        ),
                        regional_prices=regional_prices,
                    )
                )
        return offers

    def _offer(
        self,
        instance: AwsInstanceCatalogEntry,
        *,
        region: str,
        root_volume_gib: int,
        cost_terms: SupplierCostTerms,
        regional_prices: AwsRegionalPrices | None,
        preemptible: bool = False,
        availability_zone: str = "",
    ) -> ComputeOffer:
        offer_id = f"{region}:{instance.instance_type}"
        if preemptible:
            offer_id += f":spot:{availability_zone}"
        elif availability_zone:
            offer_id += f":{availability_zone}"
        return pooled_cloud_offer(
            offer_id=offer_id,
            provider=self.provider_ref,
            cloud="aws",
            instance_type=instance.instance_type,
            region=region,
            availability_zone=availability_zone,
            preemptible=preemptible,
            cpu_millicores=instance.cpu_millicores,
            memory_mb=instance.memory_mb,
            storage_mb=root_volume_gib * 1024,
            cost_terms=cost_terms.model_copy(
                update={
                    "root_disk_hourly_micros": regional_prices.root_disk_hourly_micros(
                        root_volume_gib
                    )
                    if regional_prices is not None
                    else None,
                    "public_ipv4_hourly_micros": regional_prices.public_ipv4_hourly_micros
                    if regional_prices is not None
                    else None,
                    "setup_micros": 0,
                    "billing_minimum_seconds": 60,
                    "billing_quantum_seconds": 1,
                }
            ),
            supplier_cpu_unit=SupplierCpuUnit.Vcpu,
            supplier_cpu_count=instance.cpu_millicores // 1000,
            capability_key=":".join(
                ("aws", offer_id, DEFAULT_POOLED_NODE_ARCHITECTURE, DEFAULT_POOLED_NODE_RUNTIME)
            ),
            gpu=instance.gpu.value if instance.gpu is not None else None,
            gpu_count=instance.gpu_count,
        )

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        if request.purchases_enabled:
            aws_instance_catalog_entry(request.offer.instance_type)
        provisioner = self._provisioner(request.offer.region)
        snapshot = provisioner.ensure(
            self._spec(request),
            self._resource_ids(request),
        )
        return self._snapshot(provisioner, snapshot)

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        provisioner = self._provisioner(request.offer.region)
        snapshot = provisioner.describe(
            self._spec(request),
            self._resource_ids(request),
        )
        return self._snapshot(provisioner, snapshot)

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot:
        provisioner = self._provisioner(request.offer.region)
        capacity_request = request.model_copy(
            update={
                "desired_machines": desired_machines,
                "max_machines": max_machines,
            }
        )
        spec = self._spec(capacity_request)
        resource_ids = self._resource_ids(request)
        snapshot = provisioner.ensure(spec, resource_ids)
        if not request.purchases_enabled:
            if desired_machines > snapshot.desired_nodes:
                raise ValueError("purchases are disabled for this provider")
            if snapshot.resource_ids.autoscaling_group_name is not None:
                provisioner.scale(
                    spec,
                    desired_nodes=desired_machines,
                    max_nodes=max_machines,
                )
                snapshot = provisioner.describe(spec, snapshot.resource_ids)
        return self._snapshot(provisioner, snapshot)

    def release_machine(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderUnitSnapshot:
        provisioner = self._provisioner(request.offer.region)
        spec = self._spec(request)
        desired_nodes = spec.desired_nodes
        if not request.purchases_enabled:
            observed = provisioner.ensure(spec, self._resource_ids(request))
            desired_nodes = min(desired_nodes, observed.desired_nodes)
        provisioner.scale(spec, desired_nodes=desired_nodes, max_nodes=spec.max_nodes)
        provisioner.release_instance(spec, provider_instance_id)
        return self._snapshot(provisioner, provisioner.describe(spec, self._resource_ids(request)))

    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        provisioner = self._provisioner(request.offer.region)
        return self._snapshot(
            provisioner,
            provisioner.delete(
                self._spec(request),
                self._resource_ids(request),
            ),
        )

    def machine_storage_destroyed(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        return self._provisioner(request.offer.region).machine_storage_destroyed(
            self._spec(request),
            provider_instance_id,
            storage_volume_ids,
        )

    @staticmethod
    def _snapshot(
        provisioner: AwsManagedPoolProvisioner,
        snapshot: AwsManagedPoolSnapshot,
    ) -> ProviderUnitSnapshot:
        details = provisioner.instance_details(
            tuple(instance.instance_id for instance in snapshot.instances)
        )
        return _snapshot(snapshot, details=details)

    def _provisioner(self, region: str) -> AwsManagedPoolProvisioner:
        target = self._target(region)
        return AwsManagedPoolProvisioner.assume(target, client_provider=self.client_provider)

    def _network(self, region: str) -> AwsAccountNetwork:
        network = self.networks.get(region)
        if network is None:
            raise ValueError(f"AWS managed pool network is not configured for {region!r}")
        return network

    def _target(self, region: str) -> AwsAccountConnectionTarget:
        return self.connection.model_copy(
            update={"region": region, "network": self._network(region)}
        )

    def _spec(self, request: ProviderUnitRequest) -> AwsManagedPoolSpec:
        artifacts = self.binaries_by_region.get(request.offer.region)
        if artifacts is None:
            raise ValueError(
                f"AWS managed pool artifacts are not configured for {request.offer.region!r}"
            )
        ami_id = artifacts.gpu_ami_id if request.offer.gpu_count > 0 else artifacts.cpu_ami_id
        if ami_id is None:
            raise ValueError(f"AWS managed pool AMI is not configured for {request.offer.region!r}")
        network = self._network(request.offer.region)
        return AwsManagedPoolSpec(
            workspace_id=request.workspace_id,
            unit_name=request.unit_name,
            region=request.offer.region,
            instance_type=request.offer.instance_type,
            preemptible=request.offer.preemptible,
            purchases_enabled=request.purchases_enabled,
            availability_zone=request.offer.availability_zone,
            ami_id=ami_id,
            desired_nodes=request.desired_machines,
            max_nodes=request.max_machines,
            root_volume_gib=request.root_volume_gib,
            node_instance_profile_arn=self.connection.node_instance_profile_arn,
            vpc_id=network.vpc_id,
            subnet_ids=network.subnet_ids,
            security_group_id=network.security_group_id,
            bootstrap=AwsManagedPoolBootstrap(
                control_plane_url=request.bootstrap.control_plane_url,
                enrollment_request_id=request.bootstrap.enrollment_request_id,
                agent_version=request.bootstrap.agent_version,
                agent_sha256=request.bootstrap.agent_sha256,
                agent_binary_url=request.bootstrap.agent_binary_url,
                gpu_count=request.offer.gpu_count,
            ),
        )

    @staticmethod
    def _resource_ids(request: ProviderUnitRequest) -> AwsManagedPoolResourceIds:
        if not request.provider_state.attributes:
            return AwsManagedPoolResourceIds()
        try:
            return AwsManagedPoolResourceIds.model_validate(request.provider_state.attributes)
        except ValidationError:
            # Provider state is an opaque provider-owned checkpoint. A shape this
            # provider no longer recognizes must fall back to discovery by tag
            # rather than degrade the pool forever on an unreadable checkpoint.
            return AwsManagedPoolResourceIds()


def _snapshot(
    snapshot: AwsManagedPoolSnapshot,
    *,
    details: Mapping[str, AwsManagedPoolInstanceDetails],
) -> ProviderUnitSnapshot:
    instances = [
        ProviderUnitInstance(
            provider_instance_id=instance.instance_id,
            status=(
                ProviderMachineStatus.Active
                if instance.lifecycle_state == "InService" and instance.health_status == "Healthy"
                else ProviderMachineStatus.Pending
            ),
            availability_zone=details[instance.instance_id].availability_zone,
            storage_volume_ids=details[instance.instance_id].storage_volume_ids,
            booted_template_version=instance.booted_host_revision,
        )
        for instance in snapshot.instances
    ]
    return ProviderUnitSnapshot(
        phase=_PHASES[snapshot.phase],
        resource_id=snapshot.resource_ids.autoscaling_group_name or "",
        desired_machines=snapshot.desired_nodes,
        max_machines=snapshot.max_nodes,
        observed_machines=len(instances),
        last_capacity_failure_at=snapshot.last_capacity_failure_at,
        instances=instances,
        current_template_version=snapshot.current_host_revision,
        provider_state=ComputeUnitProviderState(
            resource_id=snapshot.resource_ids.autoscaling_group_name or "",
            attributes=snapshot.resource_ids.model_dump(mode="json"),
        ),
    )


__all__ = ["AwsConnectedAccountPooledProvider"]
