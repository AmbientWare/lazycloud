from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.compute import (
    ComputeCapacityOperationRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.types import DatabaseSession
from shared.capacity import (
    CapacityOwnerKind,
    CapacityOwnerSource,
)
from shared.compute_policy import (
    ENDED_UNIT_PHASES,
    ComputeCapacityMode,
    ComputeResourceRequirements,
    ComputeUnitPhase,
    ComputeUnitProviderState,
    ComputeUnitRecord,
    ComputeUnitVisibility,
)
from shared.errors import (
    ConflictError,
    InvalidInputError,
    UpstreamUnavailableError,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import utc_now

from compute.capacity_errors import CapacityReservationConflictError
from compute.context import ComputeContext
from compute.fleet_reserves import with_retained_machines
from compute.offers import (
    ComputeOffer,
    OfferRequest,
    choose_offer,
)
from compute.pool_provider import ManagedComputeLaunchError, PoolProviderService
from compute.provider_machines import _reservation_open
from compute.providers import (
    ResolvedComputeProvider,
    internal_unit_identity,
)
from compute.unit_scaling import UnitScalingService


@dataclass(frozen=True, slots=True)
class _PooledCapacityBaseline:
    initial_machines: int
    min_machines: int
    min_free_cpu_millicores: int
    min_free_memory_mib: int


def _previous_policy_floor(current: ComputeUnitRecord | None) -> int:
    """The initial floor also holds paid capacity and must be released with the minimum."""
    if current is None:
        return 0
    return max(current.min_machines, current.initial_machines, 0)


def _policy_owned_desired_machines(
    *,
    current_desired: int,
    previous_floor: int,
    floor: int,
    ceiling: int,
) -> int:
    """Release only the old policy floor; preserve capacity acquired for demand."""

    released = max(previous_floor - floor, 0)
    return min(max(current_desired - released, floor), ceiling)


def _policy_owned_scale(pool: ComputeUnitRecord) -> None:
    """Workspace policy owns the zero-capacity intent; no extra scale guard applies."""
    del pool


@dataclass(frozen=True, slots=True)
class UnitProvisioningService:
    context: ComputeContext
    providers: PoolProviderService
    scaling: UnitScalingService

    def prepare_pooled_offer(
        self,
        *,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        requirements: ComputeResourceRequirements,
    ) -> ComputeUnitRecord:
        policy = provider.policy
        if policy is None or provider.pooled is None or offer.provider != provider.ref:
            raise InvalidInputError("offer does not belong to a pooled provider")
        if rejection := self.providers.pooled_offer_rejection(
            provider, offer, preemptible=requirements.preemptible
        ):
            raise InvalidInputError(rejection)
        return self.prepare_unit(
            provider=provider,
            offer=offer,
            requirements=requirements,
            desired_machines=0,
            root_volume_gib=policy.root_volume_gib,
            idle_timeout_seconds=policy.idle_timeout_seconds,
            baseline=None,
        )

    def reconcile_aws_default_capacity(
        self,
        *,
        workspace: str,
        region: str,
        instance_type: str,
        initial_machines: int,
        min_machines: int,
        min_free_cpu_millicores: int,
        min_free_memory_mib: int,
        root_volume_gib: int,
        idle_timeout_seconds: int,
    ) -> ComputeUnitRecord:
        """Reconcile the permanent CPU floor for an AWS-default workspace."""
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(
                workspace_id
            )
        if connection is None:
            raise UpstreamUnavailableError("AWS baseline connection is unavailable")
        pool = self.prepare_pooled_capacity(
            workspace=workspace,
            requirements=ComputeResourceRequirements(),
            region=region,
            desired_machines=initial_machines,
            root_volume_gib=root_volume_gib,
            idle_timeout_seconds=idle_timeout_seconds,
            allowed_instance_types=(instance_type,),
            provider_ref=f"aws:{connection.id}",
            baseline=_PooledCapacityBaseline(
                initial_machines=initial_machines,
                min_machines=min_machines,
                min_free_cpu_millicores=min_free_cpu_millicores,
                min_free_memory_mib=min_free_memory_mib,
            ),
        )
        if pool.desired_machines > 0 or pool.observed_machines == 0:
            return pool
        # A lowered floor that reaches zero has to run through the guarded scale
        # owner: it is what releases open capacity operations and retires the
        # sizing state. Writing a zero record alone leaves both behind, and the
        # sizing reconciler raises the machine straight back. This runs after the
        # preparing session has closed; `scale_internal_unit` takes the mutation
        # lease and locks the same row.
        return self.scaling.scale_internal_unit(
            pool.workspace_id,
            pool.capacity_owner_id,
            0,
            before_mutation=_policy_owned_scale,
        )

    def clear_aws_default_capacity(self, *, workspace: str, release_capacity: bool) -> None:
        """Clear policy floors. Release excess capacity through the fenced scaling owner."""

        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(
                workspace_id
            )
            if connection is None:
                return
            provider_ref = f"aws:{connection.id}"
            self.clear_other_internal_pool_floors(
                session,
                workspace_id=workspace_id,
                keep_pool_id=None,
                provider_ref=provider_ref,
            )
        if not release_capacity:
            return
        with self.context.database.session() as session:
            pools = [
                unit
                for unit in ComputeUnitRepository(session).list_internal(workspace_id=workspace_id)
                if unit.provider_ref == provider_ref
            ]
            protected = {
                pool.id: ComputeProviderInstanceRepository(session).count_busy_machines(pool.id)
                for pool in pools
            }
        for pool in pools:
            if pool.phase in {ComputeUnitPhase.Deleting, ComputeUnitPhase.Deleted}:
                continue
            floor = protected[pool.id]
            if pool.desired_machines <= floor:
                continue
            self.scaling.scale_internal_unit(
                workspace_id,
                pool.capacity_owner_id,
                floor,
                before_mutation=_policy_owned_scale,
            )

    def workspace_has_ready_customer_connection(self, workspace: str) -> bool:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(
                workspace_id
            )
        return connection is not None and connection.hosts_workloads

    def prepare_pooled_capacity(
        self,
        *,
        workspace: str,
        requirements: ComputeResourceRequirements,
        region: str,
        desired_machines: int,
        root_volume_gib: int,
        idle_timeout_seconds: int = 300,
        allowed_instance_types: tuple[str, ...] = (),
        baseline: _PooledCapacityBaseline | None = None,
        provider_ref: str = "",
    ) -> ComputeUnitRecord:
        if (
            self.providers.provider_resolver is None
            or self.providers.pool_bootstrap_factory is None
        ):
            raise ManagedComputeLaunchError(
                "workspace pooled compute is not configured",
                code="provider_unavailable",
            )
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
        providers = [
            provider
            for provider in self.providers.provider_resolver.list_providers(workspace_id)
            if provider.capacity_mode is ComputeCapacityMode.Pooled
            and provider.pooled is not None
            and (not provider_ref or provider.ref == provider_ref)
        ]
        if not providers:
            raise ManagedComputeLaunchError(
                "workspace has no ready pooled compute provider",
                code="provider_unavailable",
            )
        offers: list[ComputeOffer] = []
        for provider in providers:
            pooled = provider.pooled
            if pooled is None or provider.policy is None or not provider.policy.can_purchase:
                continue
            offers.extend(
                offer
                for offer in pooled.list_offers(root_volume_gib=root_volume_gib)
                if (not region or offer.region == region)
                and self.providers.pooled_offer_rejection(
                    provider, offer, preemptible=requirements.preemptible
                )
                is None
                and (not allowed_instance_types or offer.instance_type in allowed_instance_types)
            )
        try:
            offer = choose_offer(
                offers,
                OfferRequest(
                    regions=[region] if region else [],
                    min_cpu_millicores=requirements.cpu_millicores,
                    min_memory_mb=requirements.memory_mb,
                    min_storage_mb=root_volume_gib * 1024,
                    architecture=requirements.architecture or "amd64",
                    preemptible=requirements.preemptible,
                    availability_zone=requirements.availability_zone,
                    runtime=requirements.runtime,
                    gpu=requirements.gpu,
                    min_gpu_count=requirements.gpu_count,
                    nodes=max(desired_machines, 1),
                ),
            )
        except ValueError as exc:
            raise ManagedComputeLaunchError(
                "no provider capacity matches the workload requirements and placement policy",
                code="offer_unavailable",
            ) from exc
        provider = next(item for item in providers if item.ref == offer.provider)
        return self.prepare_unit(
            provider=provider,
            offer=offer,
            requirements=requirements,
            desired_machines=desired_machines,
            root_volume_gib=root_volume_gib,
            idle_timeout_seconds=idle_timeout_seconds,
            baseline=baseline,
        )

    def prepare_unit(
        self,
        *,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        requirements: ComputeResourceRequirements,
        desired_machines: int,
        root_volume_gib: int,
        idle_timeout_seconds: int,
        baseline: _PooledCapacityBaseline | None,
        now: datetime | None = None,
    ) -> ComputeUnitRecord:
        current_time = now or utc_now()
        if provider.policy is None or provider.pooled is None:
            raise ManagedComputeLaunchError(
                "pooled compute provider policy is unavailable",
                code="provider_unavailable",
            )
        workspace_id = provider.policy.workspace_id
        unit_id, unit_name = internal_unit_identity(
            workspace_id=workspace_id,
            provider_ref=provider.ref,
            region=offer.region,
            capability_key=offer.capability_key,
            root_volume_gib=root_volume_gib,
        )
        with self.context.database.session() as session:
            unit_pool = provider.policy.placement
            unit_platform_fleet = provider.policy.platform_fleet
            repository = ComputeUnitRepository(session)
            if unit_platform_fleet:
                repository.lock_platform_capacity()
            else:
                repository.lock_capacity_workspace(provider.policy.workspace_id)
            if self.providers.provider_resolver is None:
                raise UpstreamUnavailableError("compute provider resolver is unavailable")
            provider = self.providers.provider_resolver.resolve(workspace_id, provider.ref)
            if provider.policy is None:
                raise ConflictError("provider no longer accepts this capacity purchase")
            if rejection := self.providers.pooled_offer_rejection(
                provider, offer, preemptible=requirements.preemptible
            ):
                raise ConflictError(
                    f"provider no longer accepts this capacity purchase: {rejection}"
                )
            current = repository.get_by_identity(
                workspace_id=workspace_id,
                provider_ref=provider.ref,
                region=offer.region,
                capability_key=offer.capability_key,
                root_volume_gib=root_volume_gib,
                for_update=True,
            )
            if current is not None:
                unit_id, unit_name = current.id, current.name
            if current is not None and current.phase is ComputeUnitPhase.Deleting:
                raise CapacityReservationConflictError(
                    f"compute pool {current.name!r} is finishing provider resource retirement"
                )
            if (
                current is not None
                and current.provider_state.degraded_reason is not None
                and not self.providers.failed_market_retry_ready(current, now=current_time)
            ):
                raise UpstreamUnavailableError(
                    "capacity market is cooling down or still releasing failed capacity"
                )
            if current is not None and self.providers.failed_market_retry_ready(
                current, now=current_time
            ):
                records = ComputeProviderInstanceRepository(session).list_for_pool(current.id)
                if not ComputeCapacityOperationRepository(session).list_open_for_owner(
                    current.capacity_owner_id
                ) and not any(
                    _reservation_open(record.status)
                    or (
                        (record.instance_id is not None or record.storage_volume_ids)
                        and record.provider_storage_destroyed_at is None
                    )
                    for record in records
                ):
                    current = repository.upsert(
                        current.model_copy(
                            update={
                                "phase": (
                                    current.phase
                                    if current.phase is ComputeUnitPhase.Deleted
                                    else ComputeUnitPhase.Provisioning
                                ),
                                "status": (
                                    current.status
                                    if current.phase is ComputeUnitPhase.Deleted
                                    else ComputeUnitPhase.Provisioning.value
                                ),
                                "provider_state": current.provider_state.model_copy(
                                    update={
                                        "degraded_reason": None,
                                        "degraded_at": None,
                                        "launch_attempt_baseline": max(
                                            (record.launch_attempt for record in records), default=0
                                        ),
                                    }
                                ),
                            }
                        )
                    )
                else:
                    raise UpstreamUnavailableError(
                        "failed market capacity has not finished cleanup"
                    )
            requested_machines = max(
                desired_machines,
                baseline.initial_machines if baseline is not None else 0,
                baseline.min_machines if baseline is not None else 0,
            )
            if baseline is None:
                # Demand-driven placement never shrinks a pool it did not size.
                desired = max(
                    requested_machines,
                    current.desired_machines if current is not None else 0,
                )
            else:
                desired = _policy_owned_desired_machines(
                    current_desired=current.desired_machines if current is not None else 0,
                    previous_floor=_previous_policy_floor(current),
                    floor=requested_machines,
                    ceiling=max(
                        requested_machines,
                        current.desired_machines if current is not None else 0,
                    ),
                )
                if current is not None and desired < current.desired_machines:
                    # Release only down to the machines still running work; the
                    # drain owner takes the rest as they go idle.
                    desired = max(
                        desired,
                        min(
                            ComputeProviderInstanceRepository(session).count_busy_machines(
                                current.id
                            ),
                            current.desired_machines,
                        ),
                    )
                    if provider.policy.platform_fleet:
                        desired = current.desired_machines
            maximum = max(current.max_machines if current is not None else 0, desired, 1)
            minimum = (
                baseline.min_machines
                if baseline is not None
                else current.min_machines
                if current is not None
                else 0
            )
            # Only the baseline owns the durable floors. Demand-driven placement
            # reaches the same unit and must carry them through untouched, or the
            # warm capacity a workspace paid for is erased by the next request.
            initial = (
                baseline.initial_machines
                if baseline is not None
                else current.initial_machines
                if current is not None
                else 0
            )
            free_cpu = (
                baseline.min_free_cpu_millicores
                if baseline is not None
                else current.min_free_cpu_millicores
                if current is not None
                else 0
            )
            free_memory = (
                baseline.min_free_memory_mib
                if baseline is not None
                else current.min_free_memory_mib
                if current is not None
                else 0
            )
            free_gpu = current.min_free_gpu_count if current is not None else 0
            cost_terms = offer.cost_terms
            if current is not None and current.offer_cost_terms is not None:
                recorded_terms = current.offer_cost_terms
                if (
                    cost_terms.model_copy(update={"observed_at": recorded_terms.observed_at})
                    == recorded_terms
                ):
                    cost_terms = recorded_terms
            unit = ComputeUnitRecord(
                id=current.id if current is not None else unit_id,
                capacity_owner_id=current.capacity_owner_id if current is not None else unit_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                workspace_id=workspace_id,
                name=unit_name,
                placement=unit_pool,
                platform_fleet=unit_platform_fleet,
                provider=provider.ref,
                selector=unit_name,
                source="workspace_policy",
                provider_ref=provider.ref,
                provider_connection_id=provider.connection_id,
                capacity_mode=ComputeCapacityMode.Pooled,
                visibility=ComputeUnitVisibility.Internal,
                region=offer.region,
                offer_id=offer.id,
                capability_key=offer.capability_key,
                desired_machines=desired,
                stopped_machines=(
                    min(current.stopped_machines, max(maximum - desired, 0)) if current else 0
                ),
                retiring_stopped_machines=current.retiring_stopped_machines if current else 0,
                offer_cost_terms=cost_terms,
                offer_storage_mib=offer.storage_mb,
                offer_availability_zone=offer.availability_zone,
                offer_architecture=offer.architecture,
                supplier_cpu_unit=offer.supplier_cpu_unit,
                supplier_cpu_count=offer.supplier_cpu_count,
                initial_machines=min(max(initial, minimum), maximum),
                min_machines=minimum,
                max_machines=maximum,
                replacement_machine_id=(current.replacement_machine_id if current else ""),
                replacement_template_version=(
                    current.replacement_template_version if current else ""
                ),
                scaling_enabled=True,
                # True by construction rather than by preference: this unit is
                # built from workspace policy because the workspace needed general
                # capacity, so the work it serves is whatever that workspace runs.
                # A pool created by name through `create_unit` is the one that
                # earns an opt-in, and keeps the flag for it.
                worker_cpu_millicores=offer.cpu_millicores,
                worker_memory_mib=offer.memory_mb,
                worker_gpu_type=offer.gpu or "",
                worker_gpu_count=offer.gpu_count,
                worker_runtimes=(offer.runtime,),
                worker_preemptible=offer.preemptible,
                min_free_cpu_millicores=free_cpu,
                min_free_memory_mib=free_memory,
                min_free_gpu_count=free_gpu,
                idle_drain_timeout_seconds=idle_timeout_seconds,
                root_volume_gib=root_volume_gib,
                observed_machines=current.observed_machines if current is not None else 0,
                generation=(
                    current.generation + int(current.phase is ComputeUnitPhase.Deleted)
                    if current is not None
                    else 1
                ),
                # Only completed retirement can reactivate. The new generation
                # fences provider observations from the previous resource lifetime.
                phase=(
                    current.phase
                    if current is not None and current.phase not in ENDED_UNIT_PHASES
                    else ComputeUnitPhase.Provisioning
                ),
                status=(
                    current.status
                    if current is not None and current.phase not in ENDED_UNIT_PHASES
                    else ComputeUnitPhase.Provisioning.value
                ),
                provider_state=(
                    current.provider_state if current is not None else ComputeUnitProviderState()
                ),
                created_at=current.created_at if current is not None else utc_now(),
            )
            created = current is None
            if current is None or unit != current:
                current = repository.upsert(unit)
            if baseline is not None and not unit_platform_fleet:
                self.clear_other_internal_pool_floors(
                    session,
                    workspace_id=workspace_id,
                    keep_pool_id=current.id,
                    provider_ref=provider.ref,
                )
        if self.providers.scheduler_hooks is not None:
            self.providers.scheduler_hooks.register_internal_unit(current, offer)
        self.providers.publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=WorkspaceChangeType.Created if created else WorkspaceChangeType.Updated,
            resource_id=current.id,
        )
        return current

    @staticmethod
    def clear_other_internal_pool_floors(
        session: DatabaseSession,
        *,
        workspace_id: str,
        keep_pool_id: str | None,
        provider_ref: str,
    ) -> None:
        """Release obsolete floors when the baseline moves to another capability."""
        units = ComputeUnitRepository(session)
        for unit in units.list_internal(workspace_id=workspace_id):
            if unit.id == keep_pool_id or unit.provider_ref != provider_ref:
                continue
            cleared = with_retained_machines(unit, 0)
            if cleared != unit:
                units.upsert(cleared)
