from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from database.repositories.compute import (
    ComputeOfferState,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.types import DatabaseSession
from observability.workspace_changes import WorkspaceChangePublisher
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitRecord,
)
from shared.errors import (
    DomainError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import to_utc, utc_now

from compute.capacity_errors import ProviderAuthorizationPendingError
from compute.context import ComputeContext
from compute.fleet_policy import FleetCapacityPolicy
from compute.fleet_reserves import (
    ReserveAdmission,
    reserve_admission,
)
from compute.fleet_resources import ReserveMarket
from compute.offers import ComputeOffer
from compute.provider_machines import (
    ProviderMachineReconciler,
    ProviderUnitBootstrapFactory,
    _require_internal_pooled_unit,
    _utc,
    provider_unit_request,
)
from compute.providers import (
    CapacityOwnerMutationLease,
    ComputeProviderResolver,
    ComputeSchedulerHooks,
    DirectMachineProvider,
    DirectMachineProviderRegistry,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
    ResolvedComputeProvider,
    internal_unit_identity,
)
from compute.purchase_policy import assess_fleet_purchase
from compute.reclaim import ComputeReclaimPolicy
from compute.reserve_state import FleetReserveState
from compute.source_cache_storage import SourceCacheStorageLifecycleService


class ManagedComputeLaunchError(DomainError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code)


def _require_workspace_internal_pooled_unit(
    unit: ComputeUnitRecord | None,
    *,
    workspace_id: str,
    unit_ref: str,
) -> ComputeUnitRecord:
    pooled = _require_internal_pooled_unit(unit, unit_ref=unit_ref)
    if pooled.workspace_id != workspace_id:
        raise NotFoundError(f"compute unit not found: {unit_ref}")
    return pooled


@dataclass(frozen=True, slots=True)
class PoolProviderService:
    context: ComputeContext
    provider_registry: DirectMachineProviderRegistry | None = None
    provider_resolver: ComputeProviderResolver | None = None
    pool_bootstrap_factory: ProviderUnitBootstrapFactory | None = None
    scheduler_hooks: ComputeSchedulerHooks | None = None
    workspace_changes: WorkspaceChangePublisher | None = None
    capacity_owner_mutations: CapacityOwnerMutationLease | None = None
    reclaim: ComputeReclaimPolicy = field(default_factory=ComputeReclaimPolicy)
    fleet_policy: FleetCapacityPolicy = field(default_factory=FleetCapacityPolicy)
    reserve_state: FleetReserveState | None = None
    source_cache_lifecycle: SourceCacheStorageLifecycleService = field(init=False)
    machines: ProviderMachineReconciler = field(init=False)

    def __post_init__(self) -> None:
        lifecycle = SourceCacheStorageLifecycleService(self.context)
        object.__setattr__(self, "source_cache_lifecycle", lifecycle)
        object.__setattr__(
            self,
            "machines",
            ProviderMachineReconciler(
                context=self.context,
                reclaim=self.reclaim,
                pool_bootstrap_factory=self.pool_bootstrap_factory,
                workspace_changes=self.workspace_changes,
                scheduler_hooks=self.scheduler_hooks,
                source_cache_lifecycle=lifecycle,
            ),
        )

    def pooled_offer_rejection(
        self,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        *,
        preemptible: bool,
        now: datetime | None = None,
    ) -> str | None:
        policy = provider.policy
        if policy is None or offer.provider != provider.ref or not policy.accepts(offer):
            return "provider offer is outside its approved catalog"
        if not policy.can_purchase:
            return "provider purchases are disabled"
        if not policy.platform_fleet:
            return None
        assessment = assess_fleet_purchase(
            offer, self.fleet_policy, preemptible=preemptible, now=_utc(now)
        )
        if assessment.rejection is None:
            return None
        return (
            f"{assessment.rejection.value}: cost={assessment.hourly_cost_micros} "
            f"limit={assessment.max_hourly_cost_micros} USD micros/hour"
        )

    def reserve_inventory(
        self, unit: ComputeUnitRecord, *, desired: int
    ) -> ProviderUnitSnapshot | None:
        """The provider's view of the unit's reserves, when growing it may resume one."""
        if not unit.stopped_machines or desired <= unit.desired_machines:
            return None
        provider, offer = self.resolved_internal_unit_provider(unit)
        if provider.pooled is None:
            return None
        return provider.pooled.describe_unit(self.provider_unit_request(unit, offer))

    def internal_unit_provider(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> tuple[ComputeUnitRecord, ResolvedComputeProvider, ComputeOffer]:
        unit = self.get_internal_unit(workspace_id, capacity_owner_id)
        provider, offer = self.resolved_internal_unit_provider(unit)
        return unit, provider, offer

    def resolved_internal_unit_provider(
        self,
        pool: ComputeUnitRecord,
    ) -> tuple[ResolvedComputeProvider, ComputeOffer]:
        if self.provider_resolver is None:
            raise UpstreamUnavailableError("workspace compute provider resolver is not configured")
        try:
            provider = self.provider_resolver.resolve(pool.workspace_id, pool.provider_ref)
        except ProviderAuthorizationPendingError:
            raise
        except Exception as exc:
            raise UpstreamUnavailableError(
                f"compute pool {pool.name!r} provider is unavailable"
            ) from exc
        pooled = provider.pooled
        if pooled is None:
            raise InvalidInputError(f"compute pool {pool.name!r} provider is not pooled")
        return provider, pooled.unit_offer(pool)

    @staticmethod
    def available_unit_offer(
        provider: ResolvedComputeProvider, pool: ComputeUnitRecord
    ) -> ComputeOffer:
        pooled = provider.pooled
        if pooled is None:
            raise InvalidInputError(f"compute pool {pool.name!r} provider is not pooled")
        if provider.policy is not None and not provider.policy.can_purchase:
            return pooled.unit_offer(pool)
        offer = next(
            (
                item
                for item in pooled.list_offers(root_volume_gib=pool.root_volume_gib)
                if item.id == pool.offer_id and item.region == pool.region
            ),
            None,
        )
        if offer is None:
            raise UpstreamUnavailableError(
                f"compute pool {pool.name!r} offer is no longer available"
            )
        return offer.model_copy(update={"capability_key": pool.capability_key})

    def provider_unit_request(
        self,
        pool: ComputeUnitRecord,
        offer: ComputeOffer,
    ) -> ProviderUnitRequest:
        request = provider_unit_request(self.pool_bootstrap_factory, pool, offer)
        if self.provider_resolver is None:
            raise UpstreamUnavailableError("compute provider resolver is unavailable")
        provider = self.provider_resolver.resolve(pool.workspace_id, pool.provider_ref)
        if provider.policy is None:
            raise UpstreamUnavailableError("compute provider policy is unavailable")
        request = request.model_copy(
            update={
                "purchases_enabled": (
                    provider.policy.can_purchase and pool.provider_state.degraded_reason is None
                )
            }
        )
        if pool.provider_state.degraded_reason is None:
            return request
        with self.context.database.session() as session:
            surviving = ComputeProviderInstanceRepository(session).count_surviving_for_pool(pool.id)
        return request.model_copy(
            update={"desired_machines": min(surviving, request.desired_machines)}
        )

    def pooled_providers(self, workspace_id: str) -> tuple[ResolvedComputeProvider, ...]:
        if self.provider_resolver is None:
            return ()
        return tuple(
            provider
            for provider in self.provider_resolver.list_providers(workspace_id)
            if provider.capacity_mode is ComputeCapacityMode.Pooled
        )

    def reserve_admission(self) -> ReserveAdmission:
        with self.context.database.session() as session:
            return self.read_reserve_admission(session)

    def read_reserve_admission(self, session: DatabaseSession) -> ReserveAdmission:
        publication = self.reserve_state.published() if self.reserve_state is not None else None
        release = publication.release if publication is not None else None
        rows = ComputeUnitRepository(session).stopped_reserve_units(
            worker_image=release.worker_image if release else "",
            agent_sha256=release.agent.sha256 if release and release.agent else "",
        )
        return reserve_admission(
            rows,
            targets=(
                {
                    ReserveMarket.parse(key): market.stopped_target
                    for key, market in publication.markets.items()
                }
                if publication is not None and release is not None
                else None
            ),
        )

    def reported_node_memory(self) -> dict[tuple[int, int, int], int]:
        with self.context.database.session() as session:
            return ComputeUnitRepository(session).reported_node_memory()

    def pooled_offer_owner_id(self, provider: ResolvedComputeProvider, offer: ComputeOffer) -> str:
        return self.pooled_offer_owners(provider, [offer])[offer.id]

    def pooled_offer_owners(
        self, provider: ResolvedComputeProvider, offers: list[ComputeOffer]
    ) -> dict[str, str]:
        """The unit that owns, or would own, each offer."""
        policy = provider.policy
        if policy is None or provider.pooled is None:
            raise InvalidInputError("offer does not belong to a pooled provider")
        identities: dict[str, tuple[str, str, str, str, int]] = {}
        for offer in offers:
            if offer.provider != provider.ref:
                raise InvalidInputError("offer does not belong to a pooled provider")
            identities[offer.id] = (
                policy.workspace_id,
                provider.ref,
                offer.region,
                offer.capability_key,
                policy.root_volume_gib,
            )
        if not identities:
            return {}
        with self.context.database.session() as session:
            states = ComputeUnitRepository(session).offer_states(tuple(set(identities.values())))
        return {
            offer_id: state.id
            if (state := states.get(identity)) is not None
            else internal_unit_identity(
                workspace_id=identity[0],
                provider_ref=identity[1],
                region=identity[2],
                capability_key=identity[3],
                root_volume_gib=identity[4],
            )[0]
            for offer_id, identity in identities.items()
        }

    def required_capacity_owner_mutations(self) -> CapacityOwnerMutationLease:
        if self.capacity_owner_mutations is None:
            raise UpstreamUnavailableError(
                "capacity-owner mutation lease service is not configured"
            )
        return self.capacity_owner_mutations

    def get_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> ComputeUnitRecord:
        """Read the durable pooled-provider intent for a workspace-owned pool."""

        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
        return _require_workspace_internal_pooled_unit(
            unit,
            workspace_id=workspace_id,
            unit_ref=capacity_owner_id,
        )

    def publish_change(
        self,
        *,
        workspace_id: str,
        topic: WorkspaceChangeTopic,
        change: WorkspaceChangeType,
        resource_id: str,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=workspace_id,
            topic=topic,
            change=change,
            resource_id=resource_id,
        )

    def provider_client_snapshot(
        self,
        workspace: str,
    ) -> Mapping[str, DirectMachineProvider]:
        if self.provider_registry is None:
            return {}
        return self.provider_registry.snapshot(workspace)

    def provider_client_snapshot_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> Mapping[str, DirectMachineProvider]:
        if self.provider_registry is None:
            return {}
        return self.provider_registry.snapshot_for_workspace_deletion(workspace_id)

    def mark_pooled_capacity_degraded(
        self,
        pool: ComputeUnitRecord,
        *,
        reason: str | None = None,
        preserve_deleting: bool = False,
        now: datetime | None = None,
    ) -> ComputeUnitRecord | None:
        with self.context.database.session() as session:
            repository = ComputeUnitRepository(session)
            current = repository.get(pool.id, for_update=True)
            if current is None or current.generation != pool.generation:
                return current
            provider_state = (
                current.provider_state.model_copy(
                    update={"degraded_reason": reason, "degraded_at": to_utc(now or utc_now())}
                )
                if reason is not None
                else current.provider_state
            )
            degraded = repository.apply_provider_state(
                pool.id,
                generation=pool.generation,
                observed_machines=pool.observed_machines,
                phase=(
                    ComputeUnitPhase.Deleting
                    if preserve_deleting and pool.phase is ComputeUnitPhase.Deleting
                    else ComputeUnitPhase.Degraded
                ),
                provider_state=provider_state,
            )
        if degraded is not None:
            self.publish_change(
                workspace_id=degraded.workspace_id,
                topic=WorkspaceChangeTopic.ComputeUnits,
                change=WorkspaceChangeType.Updated,
                resource_id=degraded.id,
            )
        return degraded

    def retire_proven_provider_pool_machines(
        self,
        pool: ComputeUnitRecord,
        *,
        now: datetime,
    ) -> None:
        with self.context.database.session() as session:
            machine_ids = ComputeProviderInstanceRepository(session).destroyed_machine_ids(pool.id)
        if machine_ids:
            self.machines.retire_provider_pool_machines(
                pool.workspace_id,
                pool.capacity_owner_id,
                machine_ids=machine_ids,
                reason="provider instance storage destroyed",
                now=now,
            )

    @staticmethod
    def failed_market_retry_ready(
        unit: ComputeUnitRecord | ComputeOfferState, *, now: datetime
    ) -> bool:
        return (
            unit.provider_state.degraded_reason is not None
            and unit.provider_state.degraded_at is not None
            and not unit.desired_machines
            and not unit.observed_machines
            and unit.phase in {ComputeUnitPhase.Degraded, ComputeUnitPhase.Deleted}
            and now
            >= to_utc(unit.provider_state.degraded_at)
            + timedelta(seconds=unit.registration_timeout_seconds)
        )

    @staticmethod
    def capacity_rejected_recently(
        unit: ComputeUnitRecord | ComputeOfferState, *, now: datetime
    ) -> bool:
        """Cool down rejected reserve launches without degrading machines already serving."""
        failed_at = unit.provider_state.last_capacity_failure_at
        return failed_at is not None and now < to_utc(failed_at) + timedelta(
            seconds=unit.registration_timeout_seconds
        )
