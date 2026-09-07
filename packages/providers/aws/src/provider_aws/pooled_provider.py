from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

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
from shared.compute_policy import (
    ComputeUnitProviderState,
    ComputeUnitRecord,
)
from shared.supplier_costs import SupplierCostTerms, SupplierCpuUnit
from shared.timestamps import utc_now

from .account_connection import AwsAccountConnectionTarget
from .instance_catalog import (
    AWS_INSTANCE_CATALOG,
    AwsInstanceCategory,
)
from .managed_pool import (
    AwsManagedPoolBinaries,
    AwsManagedPoolBootstrap,
    AwsManagedPoolClientProvider,
    AwsManagedPoolPhase,
    AwsManagedPoolProvisioner,
    AwsManagedPoolResourceIds,
    AwsManagedPoolSnapshot,
    AwsManagedPoolSpec,
)

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
    binaries_by_region: Mapping[str, AwsManagedPoolBinaries]
    instance_hourly_micros: Mapping[str, int]
    allowed_instance_types: frozenset[str]
    client_provider: AwsManagedPoolClientProvider

    def unit_offer(self, unit: ComputeUnitRecord) -> ComputeOffer:
        region, separator, instance_type = unit.offer_id.partition(":")
        if (
            unit.provider_ref != self.provider_ref
            or region != unit.region
            or not separator
            or not instance_type
        ):
            raise ValueError("AWS unit has invalid provider offer identity")
        return recorded_unit_offer(unit, cloud="aws", instance_type=instance_type)

    def list_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        offers: list[ComputeOffer] = []
        for region, artifacts in sorted(self.binaries_by_region.items()):
            for instance in AWS_INSTANCE_CATALOG:
                if (
                    self.allowed_instance_types
                    and instance.instance_type not in self.allowed_instance_types
                ):
                    continue
                if instance.instance_type not in self.instance_hourly_micros:
                    continue
                ami_id = (
                    artifacts.cpu_ami_id
                    if instance.kind is AwsInstanceCategory.Cpu
                    else artifacts.gpu_ami_id
                )
                if ami_id is None:
                    continue
                capability_key = ":".join(
                    (
                        "aws",
                        region,
                        instance.instance_type,
                        DEFAULT_POOLED_NODE_ARCHITECTURE,
                        DEFAULT_POOLED_NODE_RUNTIME,
                    )
                )
                offers.append(
                    pooled_cloud_offer(
                        offer_id=f"{region}:{instance.instance_type}",
                        provider=self.provider_ref,
                        cloud="aws",
                        instance_type=instance.instance_type,
                        region=region,
                        cpu_millicores=instance.cpu_millicores,
                        memory_mb=instance.memory_mb,
                        storage_mb=root_volume_gib * 1024,
                        cost_terms=SupplierCostTerms(
                            source="deployment:aws.instance_hourly_micros",
                            observed_at=utc_now(),
                            compute_hourly_micros=self.instance_hourly_micros[
                                instance.instance_type
                            ],
                            setup_micros=0,
                            billing_minimum_seconds=60,
                            billing_quantum_seconds=1,
                        ),
                        supplier_cpu_unit=SupplierCpuUnit.Vcpu,
                        supplier_cpu_count=instance.cpu_millicores // 1000,
                        capability_key=capability_key,
                        gpu=instance.gpu.value if instance.gpu is not None else None,
                        gpu_count=instance.gpu_count,
                    )
                )
        return offers

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
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
        if resource_ids.autoscaling_group_name is None:
            snapshot = provisioner.ensure(spec, resource_ids)
        else:
            provisioner.scale(
                spec,
                desired_nodes=desired_machines,
                max_nodes=max_machines,
            )
            snapshot = provisioner.describe(spec, resource_ids)
        return self._snapshot(provisioner, snapshot)

    def release_machine(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderUnitSnapshot:
        provisioner = self._provisioner(request.offer.region)
        spec = self._spec(request)
        provisioner.scale(spec, desired_nodes=spec.desired_nodes, max_nodes=spec.max_nodes)
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
        volume_ids = provisioner.storage_volume_ids(
            tuple(instance.instance_id for instance in snapshot.instances)
        )
        return _snapshot(snapshot, volume_ids=volume_ids)

    def _provisioner(self, region: str) -> AwsManagedPoolProvisioner:
        target = self.connection.model_copy(update={"region": region})
        return AwsManagedPoolProvisioner.assume(target, client_provider=self.client_provider)

    def _spec(self, request: ProviderUnitRequest) -> AwsManagedPoolSpec:
        artifacts = self.binaries_by_region.get(request.offer.region)
        if artifacts is None:
            raise ValueError(
                f"AWS managed pool artifacts are not configured for {request.offer.region!r}"
            )
        ami_id = artifacts.gpu_ami_id if request.offer.gpu_count > 0 else artifacts.cpu_ami_id
        if ami_id is None:
            raise ValueError(f"AWS managed pool AMI is not configured for {request.offer.region!r}")
        network = self.connection.network
        if network is None:
            raise ValueError("AWS account connection has no network for managed pools")
        return AwsManagedPoolSpec(
            workspace_id=request.workspace_id,
            unit_name=request.unit_name,
            region=request.offer.region,
            instance_type=request.offer.instance_type,
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
    volume_ids: Mapping[str, tuple[str, ...]],
) -> ProviderUnitSnapshot:
    instances = [
        ProviderUnitInstance(
            provider_instance_id=instance.instance_id,
            status=(
                ProviderMachineStatus.Active
                if instance.lifecycle_state == "InService" and instance.health_status == "Healthy"
                else ProviderMachineStatus.Pending
            ),
            availability_zone=instance.availability_zone,
            storage_volume_ids=volume_ids.get(instance.instance_id, ()),
            booted_template_version=instance.booted_template_version,
        )
        for instance in snapshot.instances
    ]
    return ProviderUnitSnapshot(
        phase=_PHASES[snapshot.phase],
        resource_id=snapshot.resource_ids.autoscaling_group_name or "",
        desired_machines=snapshot.desired_nodes,
        max_machines=snapshot.max_nodes,
        observed_machines=len(instances),
        instances=instances,
        current_template_version=(
            ""
            if snapshot.resource_ids.launch_template_latest_version is None
            else str(snapshot.resource_ids.launch_template_latest_version)
        ),
        provider_state=ComputeUnitProviderState(
            resource_id=snapshot.resource_ids.autoscaling_group_name or "",
            attributes=snapshot.resource_ids.model_dump(mode="json"),
        ),
    )


__all__ = ["AwsConnectedAccountPooledProvider"]
