from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from database.repositories.compute import (
    ComputeCapacityOperationRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from shared.capacity import CapacityOperationStatus
from shared.compute_policy import (
    ENDED_UNIT_PHASES,
    ComputeUnitPhase,
    ComputeUnitRecord,
)
from shared.errors import (
    ConflictError,
    DomainError,
    InvalidInputError,
    UpstreamUnavailableError,
)

from compute.context import ComputeContext
from compute.fleet_reserves import reserves_resumed
from compute.offers import (
    ComputeOffer,
    record_purchase_terms,
)
from compute.pool_provider import PoolProviderService, _require_workspace_internal_pooled_unit
from compute.provider_machines import (
    _provider_zero_capacity_converged,
    _utc,
)
from compute.providers import (
    ProviderUnitSnapshot,
)


def _zero_capacity_converged(pool: ComputeUnitRecord) -> bool:
    """Durable state already says zero, so only the provider is still in doubt."""

    return (
        pool.desired_machines == 0
        and pool.observed_machines == 0
        and pool.phase is ComputeUnitPhase.Ready
    )


@dataclass(frozen=True, slots=True)
class UnitScalingService:
    context: ComputeContext
    providers: PoolProviderService

    def scale_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        desired_machines: int,
        *,
        before_mutation: Callable[[ComputeUnitRecord], None],
        now: datetime | None = None,
    ) -> ComputeUnitRecord:
        """Serialize, guard, persist, and apply one durable provider capacity intent."""

        if desired_machines < 0:
            raise InvalidInputError("desired compute pool capacity cannot be negative")
        initial = self.providers.get_internal_unit(workspace_id, capacity_owner_id)
        mutations = self.providers.required_capacity_owner_mutations()
        try:
            with mutations.mutation_lock(initial.capacity_owner_id):
                current = self.providers.get_internal_unit(workspace_id, capacity_owner_id)
                if desired_machines >= current.desired_machines and desired_machines != 0:
                    return self.scale_internal_unit_under_lease(
                        workspace_id,
                        capacity_owner_id,
                        desired_machines,
                        before_mutation=before_mutation,
                        now=now,
                    )
                with mutations.dispatch_lock(initial.capacity_owner_id):
                    return self.scale_internal_unit_under_lease(
                        workspace_id,
                        capacity_owner_id,
                        desired_machines,
                        before_mutation=before_mutation,
                        now=now,
                    )
        except DomainError:
            raise
        except Exception as exc:
            raise UpstreamUnavailableError(
                "compute capacity-owner mutation lease is unavailable"
            ) from exc

    def scale_internal_unit_under_lease(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        desired_machines: int,
        *,
        before_mutation: Callable[[ComputeUnitRecord], None],
        now: datetime | None,
    ) -> ComputeUnitRecord:
        current_time = _utc(now)
        verify_provider_zero = False
        purchase_offer: ComputeOffer | None = None
        reserves = self.providers.reserve_inventory(
            self.providers.get_internal_unit(workspace_id, capacity_owner_id),
            desired=desired_machines,
        )
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            initial = _require_workspace_internal_pooled_unit(
                units.get_by_capacity_owner_id(capacity_owner_id),
                workspace_id=workspace_id,
                unit_ref=capacity_owner_id,
            )
            if self.providers.provider_resolver is None:
                raise UpstreamUnavailableError("compute provider resolver is unavailable")
            provider = self.providers.provider_resolver.resolve(workspace_id, initial.provider_ref)
            if provider.policy is None:
                raise UpstreamUnavailableError("compute provider policy is unavailable")
            if provider.policy.platform_fleet:
                units.lock_platform_capacity()
            else:
                units.lock_capacity_workspace(provider.policy.workspace_id)
            unit = _require_workspace_internal_pooled_unit(
                units.get_by_capacity_owner_id(capacity_owner_id, for_update=True),
                workspace_id=workspace_id,
                unit_ref=capacity_owner_id,
            )
            if unit.phase in ENDED_UNIT_PHASES:
                raise ConflictError("retired compute capacity must be prepared before scaling")
            before_mutation(unit)
            if desired_machines == 0 and ComputeProviderInstanceRepository(
                session
            ).count_busy_machines(unit.id):
                raise ConflictError("compute pool still has active workloads")
            if desired_machines > 0 and desired_machines >= unit.desired_machines:
                purchase_offer = self.providers.available_unit_offer(provider, unit)
                if rejection := self.providers.pooled_offer_rejection(
                    provider,
                    purchase_offer,
                    preemptible=unit.worker_preemptible,
                    now=current_time,
                ):
                    raise ConflictError(rejection)
            if unit.provider_state.degraded_reason is not None:
                # An explicit capacity mutation supersedes the durable degraded
                # reason and re-enables capacity restoration.
                unit = unit.model_copy(
                    update={
                        "provider_state": unit.provider_state.model_copy(
                            update={"degraded_reason": None, "degraded_at": None}
                        )
                    }
                )
            if desired_machines < unit.min_machines:
                raise InvalidInputError(
                    f"compute pool {unit!r} requires at least {unit.min_machines} machines"
                )
            maximum = max(unit.max_machines, desired_machines, 1)
            if desired_machines == 0:
                unit = unit.model_copy(
                    update={
                        "replacement_machine_id": "",
                        "replacement_template_version": "",
                    }
                )
                operations = ComputeCapacityOperationRepository(session)
                for operation in operations.list_open_for_owner(unit.capacity_owner_id):
                    operations.upsert(
                        operation.model_copy(
                            update={
                                "status": CapacityOperationStatus.Released,
                                "owns_capacity": False,
                                "release_desired_unit": 0,
                                "last_error": "",
                                "failure_count": 0,
                                "updated_at": current_time,
                            }
                        )
                    )
            if desired_machines == 0 and _zero_capacity_converged(unit):
                verify_provider_zero = True
                intent = unit
            else:
                intent = units.update_capacity(
                    unit.id,
                    expected_generation=unit.generation,
                    desired_machines=desired_machines,
                    max_machines=maximum,
                    observed_machines=unit.observed_machines,
                    phase=ComputeUnitPhase.Updating,
                    provider_state=unit.provider_state,
                    replacement_machine_id=unit.replacement_machine_id,
                    replacement_template_version=unit.replacement_template_version,
                    stopped_machines=(
                        unit.stopped_machines
                        - reserves_resumed(reserves, unit, desired=desired_machines)
                        if reserves is not None
                        else None
                    ),
                )
                if intent is None:
                    raise ConflictError(f"compute pool {unit!r} capacity intent was superseded")
                if purchase_offer is not None:
                    intent = units.upsert(record_purchase_terms(intent, purchase_offer))

        try:
            provider, offer = self.providers.resolved_internal_unit_provider(intent)
            if provider.pooled is None:
                raise UpstreamUnavailableError(f"compute pool {unit!r} provider is not pooled")
            if verify_provider_zero:
                observed = provider.pooled.describe_unit(
                    self.providers.provider_unit_request(intent, offer)
                )
                if _provider_zero_capacity_converged(observed):
                    return self.providers.machines._apply_pooled_snapshot(
                        intent,
                        offer,
                        observed,
                        provider=provider.pooled,
                        now=current_time,
                    )
                intent = self.providers.machines._persist_zero_capacity_repair(
                    intent,
                    maximum=maximum,
                    observed=observed,
                )
            provider_request = self.providers.provider_unit_request(intent, offer)
            if purchase_offer is not None:
                offer = purchase_offer
                provider_request = self.providers.provider_unit_request(intent, offer)
            snapshot = provider.pooled.set_unit_capacity(
                provider_request,
                desired_machines=provider_request.desired_machines,
                max_machines=provider_request.max_machines,
            )
            return self.providers.machines._apply_pooled_snapshot(
                intent,
                offer,
                snapshot,
                provider=provider.pooled,
                now=current_time,
            )
        except ConflictError:
            raise
        except Exception as exc:
            self.providers.mark_pooled_capacity_degraded(intent)
            if isinstance(exc, InvalidInputError | UpstreamUnavailableError):
                raise
            raise UpstreamUnavailableError(
                f"compute pool {unit!r} provider capacity update failed"
            ) from exc

    def describe_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> tuple[ComputeUnitRecord, ProviderUnitSnapshot]:
        unit, provider, offer = self.providers.internal_unit_provider(
            workspace_id, capacity_owner_id
        )
        if provider.pooled is None:
            raise RuntimeError("internal compute unit does not use pooled capacity")
        snapshot = provider.pooled.describe_unit(self.providers.provider_unit_request(unit, offer))
        updated = self.providers.machines._apply_pooled_snapshot(
            unit,
            offer,
            snapshot,
            provider=provider.pooled,
        )
        return updated, snapshot

    def inspect_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> tuple[ComputeUnitRecord, ProviderUnitSnapshot]:
        """Read provider state without entering the capacity mutation boundary."""

        unit, provider, offer = self.providers.internal_unit_provider(
            workspace_id, capacity_owner_id
        )
        if provider.pooled is None:
            raise RuntimeError("internal compute unit does not use pooled capacity")
        snapshot = provider.pooled.describe_unit(self.providers.provider_unit_request(unit, offer))
        return unit, snapshot
