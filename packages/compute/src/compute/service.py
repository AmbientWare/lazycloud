from __future__ import annotations

from dataclasses import dataclass

from observability.workspace_changes import WorkspaceChangePublisher

from compute.acquisition import CapacityAcquisitionService
from compute.capacity_recovery import CapacityRecoveryService
from compute.context import ComputeContext
from compute.fleet_operations import FleetOperations
from compute.fleet_policy import FleetCapacityPolicy
from compute.machine_service import ComputeMachineService
from compute.maintenance import CapacityMaintenanceService
from compute.pool_provider import PoolProviderService
from compute.provider_machines import ProviderUnitBootstrapFactory
from compute.providers import (
    CapacityOwnerMutationLease,
    ComputeProviderResolver,
    ComputeSchedulerHooks,
    DirectMachineProviderRegistry,
)
from compute.reclaim import ComputeReclaimPolicy
from compute.release_rollout import ComputeReleaseRolloutService
from compute.request_placement import ComputeCapacityPlacementService
from compute.reserve_machines import ReserveMachineService
from compute.reserve_planning import ReservePlanningService
from compute.reserve_state import FleetReserveState
from compute.unit_provisioning import UnitProvisioningService
from compute.unit_reconciliation import UnitReconciliationService
from compute.unit_removal import UnitRemovalService
from compute.unit_scaling import UnitScalingService
from compute.units import ComputeUnitService


@dataclass(frozen=True, slots=True)
class ComputeServices:
    providers: PoolProviderService
    units: ComputeUnitService
    provisioning: UnitProvisioningService
    scaling: UnitScalingService
    removal: UnitRemovalService
    capacity: CapacityAcquisitionService
    maintenance: CapacityMaintenanceService
    reserve_machines: ReserveMachineService
    reserves: ReservePlanningService
    reconciliation: UnitReconciliationService
    machines: ComputeMachineService
    recovery: CapacityRecoveryService
    rollouts: ComputeReleaseRolloutService
    operations: FleetOperations
    placement: ComputeCapacityPlacementService

    @classmethod
    def create(
        cls,
        context: ComputeContext,
        *,
        provider_registry: DirectMachineProviderRegistry | None = None,
        provider_resolver: ComputeProviderResolver | None = None,
        pool_bootstrap_factory: ProviderUnitBootstrapFactory | None = None,
        scheduler_hooks: ComputeSchedulerHooks | None = None,
        workspace_changes: WorkspaceChangePublisher | None = None,
        capacity_owner_mutations: CapacityOwnerMutationLease | None = None,
        reclaim: ComputeReclaimPolicy | None = None,
        fleet_policy: FleetCapacityPolicy | None = None,
        reserve_state: FleetReserveState | None = None,
    ) -> ComputeServices:
        providers = PoolProviderService(
            context,
            capacity_owner_mutations=capacity_owner_mutations,
            pool_bootstrap_factory=pool_bootstrap_factory,
            provider_registry=provider_registry,
            provider_resolver=provider_resolver,
            reserve_state=reserve_state,
            scheduler_hooks=scheduler_hooks,
            workspace_changes=workspace_changes,
            reclaim=reclaim or ComputeReclaimPolicy(),
            fleet_policy=fleet_policy or FleetCapacityPolicy(),
        )
        units = ComputeUnitService(context, providers=providers)
        scaling = UnitScalingService(context, providers=providers)
        provisioning = UnitProvisioningService(context, providers=providers, scaling=scaling)
        removal = UnitRemovalService(context, providers=providers)
        capacity = CapacityAcquisitionService(context, providers=providers)
        maintenance = CapacityMaintenanceService(context, providers=providers)
        reserve_machines = ReserveMachineService(
            context, maintenance=maintenance, providers=providers
        )
        reconciliation = UnitReconciliationService(context, capacity=capacity, providers=providers)
        reserves = ReservePlanningService(
            context,
            providers=providers,
            provisioning=provisioning,
            reconciliation=reconciliation,
            scaling=scaling,
        )
        machines = ComputeMachineService(context, providers=providers)
        recovery = CapacityRecoveryService(
            capacity, maintenance, providers, provisioning, reconciliation, reserve_machines
        )
        rollouts = ComputeReleaseRolloutService(maintenance, providers, reconciliation)
        operations = FleetOperations(providers, scaling)
        placement = ComputeCapacityPlacementService(providers, provisioning)
        return cls(
            providers=providers,
            units=units,
            provisioning=provisioning,
            scaling=scaling,
            removal=removal,
            capacity=capacity,
            maintenance=maintenance,
            reserve_machines=reserve_machines,
            reserves=reserves,
            reconciliation=reconciliation,
            machines=machines,
            recovery=recovery,
            rollouts=rollouts,
            operations=operations,
            placement=placement,
        )
