from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from math import ceil
from uuid import NAMESPACE_URL, uuid5

from database.repositories.capacity_activations import (
    CapacityActivationRepository,
    CapacityActivationSummary,
)
from database.repositories.compute import (
    ComputeCapacityOperationRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
    PlatformReserveUnitRow,
)
from database.repositories.fleet_demand import FleetDemandRepository, FleetDemandRow
from shared.capacity_lifecycle import (
    CapacityImageEvidence,
    CapacitySleepMode,
)
from shared.compute_policy import (
    ENDED_UNIT_PHASES,
    ComputeResourceRequirements,
    ComputeUnitPhase,
)
from shared.errors import (
    CapacityLimitReachedError,
    ConflictError,
    UpstreamUnavailableError,
)
from shared.fleet_capacity import ReserveMachineState
from shared.gpu import GPU_ANY
from shared.placement import product_region
from shared.releases import ActiveRelease
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now

from compute.activation_timing import reserve_forecast
from compute.capacity_errors import (
    CapacityReservationLeaseLostError,
    CapacityReservationLockContendedError,
)
from compute.context import ComputeContext
from compute.demand_forecast import HISTORY_SECONDS, DemandForecast, DemandSample
from compute.fleet_policy import (
    RESERVE_PLAN_INTERVAL_SECONDS,
    FleetReservePlan,
    FleetReserveSnapshot,
    GrowthKind,
    MarketPlanApplication,
    MarketReservePlan,
    ReserveConditions,
    ReserveGrowth,
    plan_market_reserve,
)
from compute.fleet_reserves import (
    fleet_reserve_snapshot,
    machine_capacity,
    unit_reserve_market,
    with_retained_machines,
)
from compute.fleet_resources import (
    Capacity,
    ReserveDemand,
    ReserveMarket,
    ReserveOffer,
    ReservePlacement,
)
from compute.offers import (
    ComputeOffer,
    ReservationStatus,
    cooling_regions,
)
from compute.pool_provider import PoolProviderService
from compute.providers import ResolvedComputeProvider
from compute.scheduled_forecast import backlog_container_demand, scheduled_container_demand
from compute.unit_provisioning import UnitProvisioningService, _policy_owned_scale
from compute.unit_reconciliation import UnitReconciliationService
from compute.unit_scaling import UnitScalingService

LOGGER = logging.getLogger(__name__)
_RESERVE_OWNER = str(uuid5(NAMESPACE_URL, "lazycloud:platform-warm-capacity"))


def _describe_capacity(capacity: Capacity) -> str:
    described = f"{capacity.cpu_millicores / 1000:g} vCPU/{capacity.memory_mib / 1024:g} GiB"
    return f"{described}/{capacity.gpu_count} GPU" if capacity.gpu_count else described


@dataclass(frozen=True, slots=True)
class ReservePlanningService:
    context: ComputeContext
    providers: PoolProviderService
    provisioning: UnitProvisioningService
    reconciliation: UnitReconciliationService
    scaling: UnitScalingService
    _reserve_decisions: dict[ReserveMarket, str] = field(default_factory=dict, init=False)

    def reconcile_platform_reserves(
        self,
        *,
        now: datetime,
        early: bool = False,
        workers: Sequence[SchedulerWorkerRecord] = (),
        release: ActiveRelease | None = None,
    ) -> FleetReservePlan | None:
        """Plan headroom under the fleet claim; sustained shortages can advance the next pass."""
        if self.providers.provider_resolver is None or self.providers.reserve_state is None:
            return None
        if not self.providers.reserve_state.claim_plan(early=early):
            return None
        try:
            with self.providers.required_capacity_owner_mutations().mutation_lock(_RESERVE_OWNER):
                started = time.monotonic()
                try:
                    return self.plan_reserves(now=now, workers=workers, release=release)
                finally:
                    LOGGER.info("platform reserve planning took %.3fs", time.monotonic() - started)
        except CapacityReservationLockContendedError:
            LOGGER.debug("platform reserve planning deferred: its lease is held")
            return None

    def observe_reserve_pressure(
        self, free: Mapping[ReserveMarket, Capacity], *, now: datetime
    ) -> bool:
        """Only markets with a running target can trigger an early pressure-driven plan."""
        if self.providers.reserve_state is None:
            return False
        publication = self.providers.reserve_state.published()
        if publication is None:
            return True
        targets = publication.markets
        ready = False
        for key, target in targets.items():
            market = ReserveMarket.parse(key)
            short = not free.get(market, Capacity()).covers(target.warm_target)
            ready = (
                self.providers.reserve_state.pressure_ready(
                    market,
                    under_pressure=short,
                    now=now,
                    sustained_seconds=self.providers.fleet_policy.pressure_seconds,
                )
                or ready
            )
        return ready

    def plan_reserves(
        self,
        *,
        now: datetime,
        workers: Sequence[SchedulerWorkerRecord],
        release: ActiveRelease | None,
    ) -> FleetReservePlan:
        assert (
            self.providers.provider_resolver is not None
            and self.providers.reserve_state is not None
        )
        providers = tuple(self.providers.provider_resolver.list_platform_providers())
        purchasable = frozenset(
            provider.ref
            for provider in providers
            if provider.pooled is not None
            and provider.policy is not None
            and provider.policy.can_purchase
        )
        with self.context.database.session() as session:
            rows = ComputeUnitRepository(session).platform_reserve_rows()
            activations = CapacityActivationRepository(session).summarize(
                since=now - timedelta(days=1)
            )
            demand_rows = FleetDemandRepository(session).recent(
                since=now - timedelta(seconds=HISTORY_SECONDS),
                now=now,
            )
            demand_rows += backlog_container_demand(FleetDemandRepository(session).backlog(now=now))
            forecast_until = now + timedelta(
                seconds=max(
                    self.providers.fleet_policy.total_forecast_seconds,
                    max(
                        (
                            ceil(row.p95_ready_seconds) + RESERVE_PLAN_INTERVAL_SECONDS
                            for row in activations
                            if row.p95_ready_seconds is not None
                        ),
                        default=0,
                    ),
                )
            )
            demand_rows += scheduled_container_demand(
                FleetDemandRepository(session).scheduled(now=now, until=forecast_until),
                now=now,
                until=forecast_until,
            )
        snapshot = fleet_reserve_snapshot(
            rows,
            purchasable_providers=purchasable,
            now=now,
            ready_machine_ids=frozenset(
                worker.machine_id
                for worker in workers
                if release is not None
                and release.target.accepts(worker.runtime_image, worker.agent_binary_sha256)
                and worker.request_intake_status(at=now) is SchedulerWorkerStatus.Available
            ),
            release=release,
        )
        consolidation_hosts = {
            machine
            for operation in self.providers.reserve_state.consolidations().values()
            for machine in operation.destination_machine_ids
        }
        snapshot = replace(
            snapshot,
            machines=tuple(
                replace(machine, protected=True) if machine.key in consolidation_hosts else machine
                for machine in snapshot.machines
            ),
        )
        conditions = self.reserve_conditions(
            snapshot, demand_rows, activations=activations, now=now
        )
        plan = plan_market_reserve(self.providers.fleet_policy, snapshot, conditions)
        catalog: dict[str, tuple[ResolvedComputeProvider, ComputeOffer, bool]] = {}
        if any(
            market.market not in conditions.demand
            and (
                not market.shortfall.empty
                or not market.hibernation_shortfall.empty
                or bool(market.unmet_shapes or market.unmet_stopped_shapes)
                or not market.stopped_capacity.covers(market.stopped_target)
                or bool(market.unmet_placements)
                or any(
                    machine.state is ReserveMachineState.Serving
                    and machine.containers == 0
                    and not machine.protected
                    and machine.key in conditions.lightly_used_since
                    and now - conditions.lightly_used_since[machine.key]
                    >= timedelta(seconds=self.providers.fleet_policy.consolidation_seconds)
                    for machine in snapshot.machines
                )
            )
            for market in plan.markets
        ):
            catalog = self.reserve_catalog(providers, now=now)
            with self.context.database.session() as session:
                reported_memory = ComputeUnitRepository(session).reported_node_memory()
            snapshot = replace(
                snapshot,
                offers=tuple(
                    ReserveOffer(
                        key=key,
                        market=unit_reserve_market(
                            preemptible=offer.preemptible, gpu_type=offer.gpu or ""
                        ),
                        machine=machine_capacity(
                            offer.cpu_millicores,
                            offer.memory_mb,
                            offer.gpu_count,
                            reported_memory_mib=reported_memory.get(
                                (offer.cpu_millicores, offer.memory_mb, offer.gpu_count),
                                0,
                            ),
                        ),
                        nominal_cpu_millicores=offer.cpu_millicores,
                        hourly_cost_micros=cost,
                        stopped_hourly_cost_micros=disk,
                        supports_reserve=reserve,
                        supports_hibernation=offer.supports_hibernation,
                        preference_rank=(preference,),
                        placement=ReservePlacement(
                            region=(product_region(offer.region) or offer.region),
                            zone=offer.availability_zone,
                            architecture=offer.architecture,
                            runtimes=(offer.runtime,),
                        ),
                    )
                    for preference, (key, (_, offer, reserve)) in enumerate(catalog.items())
                    if (cost := offer.cost_terms.complete_hourly_cost_micros) is not None
                    and (disk := offer.cost_terms.root_disk_hourly_micros) is not None
                ),
            )
            plan = plan_market_reserve(self.providers.fleet_policy, snapshot, conditions)
        units = {unit.id: unit for unit in rows.units}
        applied: list[MarketReservePlan] = []
        for market in plan.markets:
            self.log_market_plan(market)
            try:
                self.apply_market_plan(market, units, catalog, release=release, now=now)
                applied.append(
                    replace(market, application=MarketPlanApplication.Applied, applied_at=utc_now())
                )
            except CapacityReservationLockContendedError:
                applied.append(
                    replace(
                        market,
                        application=MarketPlanApplication.Deferred,
                        application_reason="capacity owner lease is held",
                    )
                )
                LOGGER.debug("platform reserve for %s deferred: a lease is held", market.market.key)
            except CapacityReservationLeaseLostError:
                raise
            except (CapacityLimitReachedError, ConflictError, UpstreamUnavailableError) as exc:
                applied.append(
                    replace(
                        market,
                        application=MarketPlanApplication.Deferred,
                        application_reason=exc.code,
                    )
                )
                LOGGER.warning("platform reserve for %s not applied: %s", market.market.key, exc)
            except Exception:
                applied.append(
                    replace(
                        market,
                        application=MarketPlanApplication.Failed,
                        application_reason="capacity application failed",
                    )
                )
                LOGGER.exception("platform reserve for %s failed", market.market.key)
        for unit in rows.units:
            if unit.phase is not ComputeUnitPhase.Degraded or not unit.desired:
                continue
            serving = sum(
                machine.unit_id == unit.id and machine.state is ReserveMachineState.Serving
                for machine in snapshot.machines
            )
            try:
                self.release_unclaimed_failed_capacity(unit.id, serving=serving, now=now)
            except CapacityReservationLockContendedError:
                continue
            except Exception:
                LOGGER.exception("releasing failed capacity in %s failed", unit.id)
        plan = replace(plan, markets=tuple(applied))
        self.providers.reserve_state.publish(
            plan, generated_at=now, release=release.target if release else None
        )
        return plan

    def reserve_conditions(
        self,
        snapshot: FleetReserveSnapshot,
        demand: Sequence[FleetDemandRow],
        *,
        now: datetime,
        activations: Sequence[CapacityActivationSummary] = (),
    ) -> ReserveConditions:
        assert self.providers.reserve_state is not None
        markets = {unit.unit_id: unit.market for unit in snapshot.units}
        samples: dict[ReserveMarket, list[DemandSample]] = {}
        scheduled: dict[ReserveMarket, list[DemandSample]] = {}
        pending: dict[ReserveMarket, Capacity] = {}
        pending_shapes: dict[ReserveMarket, set[Capacity]] = {}
        placement_rows: dict[
            tuple[ReserveMarket, ReservePlacement, Capacity], list[FleetDemandRow]
        ] = {}

        def forecast(
            market: ReserveMarket,
            history: Sequence[DemandSample],
            future: Sequence[DemandSample],
            waiting: Capacity,
            placement: ReservePlacement = ReservePlacement(),
        ) -> DemandForecast:
            return reserve_forecast(
                self.providers.fleet_policy,
                snapshot,
                activations,
                market=market,
                history=history,
                now=now,
                pending=waiting,
                scheduled=future,
                placement=placement,
                pending_shapes=tuple(pending_shapes.get(market, ())),
            )

        for row in demand:
            cards = tuple(
                card
                for card in self.providers.fleet_policy.gpu
                if card in row.gpu_types or GPU_ANY in row.gpu_types
            )
            if row.gpu_count and not cards:
                continue

            def card_rank(card: str) -> tuple[bool, float, str]:
                candidates = [
                    unit for unit in snapshot.units if unit.market.gpu_type == card and unit.enabled
                ]
                return (
                    not any(unit.desired or unit.stopped for unit in candidates),
                    min(
                        (
                            unit.hourly_cost_micros / unit.machine.gpu_count
                            for unit in candidates
                            if unit.hourly_cost_micros is not None and unit.machine.gpu_count
                        ),
                        default=float("inf"),
                    ),
                    card,
                )

            card = min(cards, key=card_rank) if row.gpu_count else ""
            market = ReserveMarket(
                preemptible=row.preemptible if not card else False, gpu_type=card
            )
            capacity = Capacity(row.cpu_millicores, row.memory_mib, row.gpu_count)
            count = row.count if row.observed_at > now else row.count - row.pending_count
            if count:
                (scheduled if row.observed_at > now else samples).setdefault(market, []).append(
                    DemandSample(
                        row.observed_at, capacity, count, duration_seconds=row.duration_seconds
                    )
                )
            if row.pending_count:
                pending[market] = pending.get(market, Capacity()) + capacity * row.pending_count
                pending_shapes.setdefault(market, set()).add(capacity)
            placement = ReservePlacement(
                region=row.region,
                zone=row.availability_zone,
                architecture=row.architecture,
                runtime=row.runtime,
            )
            placement_rows.setdefault((market, placement, capacity), []).append(row)
        forecasts = {
            market: forecast(
                market,
                samples.get(market, ()),
                scheduled.get(market, ()),
                pending.get(market, Capacity()),
            )
            for market in samples.keys() | scheduled.keys() | pending.keys()
        }
        placement_demands: dict[ReserveMarket, list[ReserveDemand]] = {}
        for (market, placement, capacity), rows in placement_rows.items():
            placement_forecast = forecast(
                market,
                tuple(
                    DemandSample(
                        row.observed_at,
                        capacity,
                        row.count - row.pending_count,
                        duration_seconds=row.duration_seconds,
                    )
                    for row in rows
                    if row.observed_at <= now and row.count > row.pending_count
                ),
                tuple(
                    DemandSample(
                        row.observed_at, capacity, row.count, duration_seconds=row.duration_seconds
                    )
                    for row in rows
                    if row.observed_at > now
                ),
                capacity * sum(row.pending_count for row in rows),
                placement,
            )
            count = max(
                (
                    ceil(total / shape)
                    for total, shape in (
                        (placement_forecast.warm.cpu_millicores, capacity.cpu_millicores),
                        (placement_forecast.warm.memory_mib, capacity.memory_mib),
                        (placement_forecast.warm.gpu_count, capacity.gpu_count),
                    )
                    if shape
                ),
                default=0,
            )
            if count:
                placement_demands.setdefault(market, []).append(
                    ReserveDemand(capacity, placement, count)
                )
        publication = self.providers.reserve_state.published()
        return ReserveConditions(
            now=now,
            lightly_used_since=publication.lightly_used_since if publication is not None else {},
            recovering=frozenset(
                markets[machine.unit_id]
                for machine in snapshot.machines
                if machine.protected and machine.state is ReserveMachineState.Draining
            ),
            consolidating=self.providers.reserve_state.cooling_markets(),
            demand=frozenset(pending),
            forecasts=forecasts,
            request_shapes={
                market: tuple(set(value.request_shapes) | pending_shapes.get(market, set()))
                for market, value in forecasts.items()
            },
            placement_demands={
                market: tuple(values) for market, values in placement_demands.items()
            },
        )

    def log_market_plan(self, plan: MarketReservePlan) -> None:
        # Every replica plans in turn; a line per change rather than per pass.
        growth = (
            ", ".join(
                f"{action.kind.value} {action.count} in {action.unit_id or action.offer_key}"
                for action in plan.growth
            )
            or "none"
        )
        decision = (
            f"{'quiet' if plan.quiet else 'loaded'}, load {_describe_capacity(plan.load)}, "
            "running free "
            f"{_describe_capacity(plan.warm_free)} of {_describe_capacity(plan.warm_target)}, "
            f"reserve ready {_describe_capacity(plan.stopped_ready)} of "
            f"{_describe_capacity(plan.stopped_target)}, "
            f"preparing {_describe_capacity(plan.stopped_pending)}, "
            f"image saved {_describe_capacity(plan.image_saved_capacity)}, "
            f"hibernation unverified {_describe_capacity(plan.hibernation_unverified_capacity)}, "
            f"growth {growth}, "
            f"unmet shapes {plan.unmet_shapes}, stopped shapes {plan.unmet_stopped_shapes}, "
            f"stopped shortfall {_describe_capacity(plan.stopped_shortfall)}, "
            f"reason {plan.reason}, "
            f"consolidating {plan.consolidate or plan.consolidation_candidate or 'none'}"
        )
        if decision != self._reserve_decisions.get(plan.market):
            self._reserve_decisions[plan.market] = decision
            LOGGER.info("platform reserve for %s: %s", plan.market.key, decision)

    def apply_market_plan(
        self,
        plan: MarketReservePlan,
        units: Mapping[str, PlatformReserveUnitRow],
        catalog: Mapping[str, tuple[ResolvedComputeProvider, ComputeOffer, bool]],
        *,
        release: ActiveRelease | None,
        now: datetime,
    ) -> None:
        for unit_id, retained in sorted(
            plan.retained.items(), key=lambda item: item[1] <= units[item[0]].retained
        ):
            if retained != units[unit_id].retained:
                self.retain_machines(unit_id, retained)
        running_targets = {identity: unit.desired for identity, unit in units.items()}
        stopped_targets = {identity: unit.stopped for identity, unit in units.items()}
        preparations: dict[str, int] = {}
        for action in plan.growth:
            identity = action.unit_id
            if not identity:
                provider, offer, _ = catalog[action.offer_key]
                identity = self.providers.pooled_offer_owner_id(provider, offer)
            targets = stopped_targets if action.kind is GrowthKind.Prepare else running_targets
            target = targets.get(identity, 0) + action.count
            targets[identity] = target
            if action.kind is GrowthKind.Prepare:
                preparations[identity] = preparations.get(identity, 0) + action.count
            if action.kind is GrowthKind.Resume:
                stopped_targets[identity] = max(stopped_targets.get(identity, 0) - action.count, 0)
            self.apply_reserve_growth(plan.market, action, units, catalog, target=target, now=now)
        retiring: dict[str, int] = {}
        for unit_id, stopped in plan.stopped.items():
            resuming = sum(
                action.count
                for action in plan.growth
                if action.kind is GrowthKind.Resume and action.unit_id == unit_id
            )
            if stopped + resuming < units[unit_id].stopped:
                retiring[unit_id] = stopped + preparations.get(unit_id, 0)
        if retiring:
            self.set_stopped_reserves(retiring, plan=plan, release=release, now=now)

    def apply_reserve_growth(
        self,
        market: ReserveMarket,
        action: ReserveGrowth,
        units: Mapping[str, PlatformReserveUnitRow],
        catalog: Mapping[str, tuple[ResolvedComputeProvider, ComputeOffer, bool]],
        *,
        target: int,
        now: datetime,
    ) -> None:
        if action.kind is GrowthKind.Resume:
            with self.providers.required_capacity_owner_mutations().mutation_lock(action.unit_id):
                current = self.providers.get_internal_unit(
                    units[action.unit_id].workspace_id, action.unit_id
                )
                if current.desired_machines >= target:
                    return
                self.scaling.scale_internal_unit(
                    current.workspace_id,
                    current.id,
                    target,
                    before_mutation=_policy_owned_scale,
                    now=now,
                )
            return
        provider, offer, supports_reserve = catalog[action.offer_key]
        reserve = action.kind is GrowthKind.Prepare
        if reserve and not supports_reserve:
            raise ConflictError("selected offer cannot prepare a stopped reserve")
        policy = provider.policy
        assert policy is not None
        owner_id = self.providers.pooled_offer_owner_id(provider, offer)
        with self.providers.required_capacity_owner_mutations().mutation_lock(owner_id):
            unit = self.provisioning.prepare_unit(
                provider=provider,
                offer=offer,
                requirements=ComputeResourceRequirements(
                    preemptible=market.preemptible,
                    gpu=[offer.gpu] if offer.gpu else [],
                    gpu_count=offer.gpu_count,
                ),
                desired_machines=0,
                root_volume_gib=policy.root_volume_gib,
                idle_timeout_seconds=policy.idle_timeout_seconds,
                baseline=None,
                now=now,
            )
            if reserve:
                self.add_stopped_reserve(unit.id, target=target, now=now)
            elif unit.desired_machines < target:
                self.scaling.scale_internal_unit(
                    unit.workspace_id,
                    unit.capacity_owner_id,
                    target,
                    before_mutation=_policy_owned_scale,
                    now=now,
                )

    def reserve_catalog(
        self,
        providers: Sequence[ResolvedComputeProvider],
        *,
        now: datetime,
    ) -> dict[str, tuple[ResolvedComputeProvider, ComputeOffer, bool]]:
        candidates: dict[str, tuple[ResolvedComputeProvider, ComputeOffer, bool]] = {}
        for provider in providers:
            policy = provider.policy
            if provider.pooled is None or policy is None or not policy.can_purchase:
                continue
            for reserve in (False, True):
                listing = (
                    provider.pooled.list_reserve_offers(root_volume_gib=policy.root_volume_gib)
                    if reserve
                    else provider.pooled.list_offers(root_volume_gib=policy.root_volume_gib)
                )
                for offer in listing:
                    if (
                        offer.provider != provider.ref
                        or offer.storage_mb < policy.root_volume_gib * 1024
                        or offer.cost_terms.complete_hourly_cost_micros is None
                        or self.providers.pooled_offer_rejection(
                            provider,
                            offer,
                            preemptible=offer.preemptible,
                            now=now,
                        )
                        is not None
                    ):
                        continue
                    candidates[f"{provider.ref}:{offer.id}:{int(reserve)}"] = (
                        provider,
                        offer,
                        reserve,
                    )
        identities = {
            key: (
                provider.policy.workspace_id,
                provider.ref,
                offer.region,
                offer.capability_key,
                provider.policy.root_volume_gib,
            )
            for key, (provider, offer, _) in candidates.items()
            if provider.policy is not None
        }
        with self.context.database.session() as session:
            states = ComputeUnitRepository(session).offer_states(tuple(set(identities.values())))
        eligible = {
            key: value
            for key, value in candidates.items()
            if (state := states.get(identities[key])) is None
            or (
                state.phase is not ComputeUnitPhase.Deleting
                and not self.providers.capacity_rejected_recently(state, now=now)
                and (
                    state.provider_state.degraded_reason is None
                    or self.providers.failed_market_retry_ready(state, now=now)
                )
            )
        }
        identity_offers = {identities[key]: offer for key, (_, offer, _) in candidates.items()}
        short_regions = cooling_regions(
            (
                (
                    identity_offers[identity].market.cloud,
                    identity[2],
                    state.provider_state.last_capacity_failure_at,
                )
                for identity, state in states.items()
            ),
            now=now,
        )
        healthy_markets = {
            (offer.preemptible, offer.gpu or "", reserve)
            for _, offer, reserve in eligible.values()
            if (offer.market.cloud, offer.region) not in short_regions
        }
        eligible = {
            key: value
            for key, value in eligible.items()
            if (value[1].market.cloud, value[1].region) not in short_regions
            or (value[1].preemptible, value[1].gpu or "", value[2]) not in healthy_markets
        }
        zone_counts: dict[tuple[str, str, str], int] = {}
        seen: set[str] = set()
        for key, (provider, offer, _) in candidates.items():
            state = states.get(identities[key])
            if state is None or state.id in seen:
                continue
            seen.add(state.id)
            zone = (provider.ref, offer.region, offer.availability_zone)
            zone_counts[zone] = zone_counts.get(zone, 0) + max(
                state.desired_machines, state.observed_machines
            )
        return dict(
            sorted(
                eligible.items(),
                key=lambda item: (
                    item[1][0].policy.region_rank(item[1][1].region)
                    if item[1][0].policy is not None
                    else 0,
                    zone_counts.get(
                        (item[1][0].ref, item[1][1].region, item[1][1].availability_zone), 0
                    ),
                    item[0],
                ),
            )
        )

    def retain_machines(self, unit_id: str, retained: int) -> None:
        """Update the retained floor under the same lease used by idle retirement."""
        with (
            self.providers.required_capacity_owner_mutations().mutation_lock(unit_id),
            self.context.database.session() as session,
        ):
            units = ComputeUnitRepository(session)
            current = units.get(unit_id, for_update=True)
            if current is None or current.phase in ENDED_UNIT_PHASES:
                return
            updated = with_retained_machines(current, min(retained, current.desired_machines))
            if updated == current:
                return
            updated = units.upsert(updated)
        # The drain reads the count from the unit's hot state.
        if self.providers.scheduler_hooks is not None:
            provider, offer = self.providers.resolved_internal_unit_provider(updated)
            if provider.pooled is not None:
                self.providers.scheduler_hooks.register_internal_unit(updated, offer)

    def set_stopped_reserves(
        self,
        targets: Mapping[str, int],
        *,
        plan: MarketReservePlan,
        release: ActiveRelease | None,
        now: datetime,
    ) -> None:
        if release is None:
            return
        changed: list[str] = []
        with ExitStack() as leases:
            for unit_id in sorted(targets):
                leases.enter_context(
                    self.providers.required_capacity_owner_mutations().mutation_lock(unit_id)
                )
            with self.context.database.session() as session:
                units = ComputeUnitRepository(session)
                units.lock_platform_capacity()
                rows = units.platform_reserve_rows()
                shapes = {row.id: row for row in rows.units}
                ready_counts: dict[str, int] = {}
                fast_counts: dict[str, int] = {}
                protected: set[str] = set()
                available = Capacity()
                fast = Capacity()
                for instance in rows.instances:
                    shape = shapes[instance.unit_id]
                    if instance.status == ReservationStatus.Stopped.value and instance.protected:
                        protected.add(instance.unit_id)
                    if (
                        instance.status != ReservationStatus.Stopped.value
                        or instance.missing
                        or unit_reserve_market(
                            preemptible=shape.preemptible, gpu_type=shape.gpu_type
                        )
                        != plan.market
                        or not release.target.accepts(
                            instance.prepared_worker_image, instance.prepared_agent_sha256
                        )
                    ):
                        continue
                    ready_counts[instance.unit_id] = ready_counts.get(instance.unit_id, 0) + 1
                    if instance.image_evidence is CapacityImageEvidence.Saved or (
                        instance.image_evidence is CapacityImageEvidence.Unknown
                        and instance.sleep_accepted_mode is CapacitySleepMode.Hibernate
                    ):
                        fast_counts[instance.unit_id] = fast_counts.get(instance.unit_id, 0) + 1
                for unit_id, count in ready_counts.items():
                    shape = shapes[unit_id]
                    ready_counts[unit_id] = min(
                        shape.stopped, max(count - shape.retiring_stopped, 0)
                    )
                    fast_counts[unit_id] = min(
                        shape.stopped, max(fast_counts.get(unit_id, 0) - shape.retiring_stopped, 0)
                    )
                    capacity = machine_capacity(
                        shape.cpu_millicores,
                        shape.memory_mib,
                        shape.gpu_count,
                        reported_memory_mib=shape.reported_memory_mib,
                    )
                    available += capacity * ready_counts[unit_id]
                    fast += capacity * fast_counts[unit_id]
                required = available.lower(plan.stopped_target)
                required_fast = fast.lower(plan.hibernation_target)
                for unit_id, stopped in targets.items():
                    current = units.get(unit_id, for_update=True)
                    if (
                        current is None
                        or stopped >= current.stopped_machines
                        or current.maintenance_active
                        or unit_id in protected
                    ):
                        continue
                    shape = shapes[unit_id]
                    capacity = machine_capacity(
                        shape.cpu_millicores,
                        shape.memory_mib,
                        shape.gpu_count,
                        reported_memory_mib=shape.reported_memory_mib,
                    )
                    removing = current.stopped_machines - stopped
                    removed = capacity * min(removing, ready_counts.get(unit_id, 0))
                    removed_fast = capacity * min(removing, fast_counts.get(unit_id, 0))
                    if not (available - removed).covers(required) or not (
                        fast - removed_fast
                    ).covers(required_fast):
                        continue
                    available -= removed
                    fast -= removed_fast
                    units.upsert(
                        current.model_copy(
                            update={
                                "stopped_machines": stopped,
                                "retiring_stopped_machines": current.retiring_stopped_machines
                                + removing,
                                "generation": current.generation + 1,
                            }
                        )
                    )
                    changed.append(unit_id)
        for unit_id in changed:
            LOGGER.info("retiring stopped reserves in %s down to %d", unit_id, targets[unit_id])
            self.reconciliation.reconcile_unit_capacity(unit_id, now=now)

    def add_stopped_reserve(self, unit_id: str, *, target: int, now: datetime) -> None:
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            units.lock_platform_capacity()
            current = units.get(unit_id, for_update=True)
            if current is None or current.phase in ENDED_UNIT_PHASES:
                return
            if current.retiring_stopped_machines:
                return
            if current.stopped_machines >= target:
                return
            units.upsert(
                current.model_copy(
                    update={
                        "stopped_machines": target,
                        "max_machines": max(
                            current.max_machines,
                            current.desired_machines + target,
                        ),
                        "generation": current.generation + 1,
                    }
                )
            )
        self.reconciliation.reconcile_unit_capacity(unit_id, now=now)

    def release_unclaimed_failed_capacity(
        self, unit_id: str, *, serving: int, now: datetime
    ) -> None:
        """Give back what a failed market bought but never served, keeping what serves.

        `serving` comes from the planner's snapshot of the same pass.
        """
        mutations = self.providers.required_capacity_owner_mutations()
        with (
            mutations.mutation_lock(unit_id),
            mutations.dispatch_lock(unit_id),
        ):
            if mutations.has_open_reservations(unit_id):
                return
            with self.context.database.session() as session:
                repository = ComputeUnitRepository(session)
                repository.lock_platform_capacity()
                current = repository.get(unit_id, for_update=True)
                if (
                    current is None
                    or current.phase is not ComputeUnitPhase.Degraded
                    or current.desired_machines == 0
                    or ComputeCapacityOperationRepository(session).list_open_for_owner(
                        current.capacity_owner_id
                    )
                    or ComputeProviderInstanceRepository(session).count_busy_machines(current.id)
                ):
                    return
                retained = min(current.desired_machines, current.min_machines, serving)
                intent = with_retained_machines(current, retained).model_copy(
                    update={
                        "desired_machines": retained,
                        "replacement_machine_id": "",
                        "replacement_template_version": "",
                        "provider_state": current.provider_state.model_copy(
                            update={
                                "degraded_reason": current.provider_state.degraded_reason
                                or "provider_acquisition_rejected",
                                "degraded_at": current.provider_state.degraded_at or now,
                            }
                        ),
                    }
                )
                if intent != current:
                    intent = repository.upsert(
                        intent.model_copy(update={"generation": current.generation + 1})
                    )
            # Preserve degradation so cancellation cannot re-enable purchases.
            provider, offer = self.providers.resolved_internal_unit_provider(intent)
            if provider.pooled is None:
                raise UpstreamUnavailableError("failed capacity provider is not pooled")
            request = self.providers.provider_unit_request(intent, offer)
            snapshot = provider.pooled.set_unit_capacity(
                request,
                desired_machines=request.desired_machines,
                max_machines=request.max_machines,
            )
            self.providers.machines._apply_pooled_snapshot(
                intent, offer, snapshot, provider=provider.pooled, now=now
            )
