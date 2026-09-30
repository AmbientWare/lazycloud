from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.capacity_recovery import CapacityRecoveryRepository
from database.repositories.compute import (
    ComputeCapacityOperationRepository,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
)
from database.types import DatabaseSession
from shared.capacity import CapacityReleaseRequest
from shared.compute_enrollment import (
    ComputeMachineEnrollmentStatus,
    MachineBootstrapFailureReason,
)
from shared.compute_policy import (
    ENDED_UNIT_PHASES,
    ComputeUnitPhase,
    ComputeUnitRecord,
    ComputeUnitVisibility,
)
from shared.compute_reconciliation import (
    COMPUTE_RECONCILIATION_BATCH_SIZE,
    ComputeReconciliationKind,
)
from shared.containers import LIVE_CONTAINER_STATUSES, ContainerStatus
from shared.errors import (
    ConflictError,
    NotFoundError,
    StaleProviderStateError,
    UpstreamUnavailableError,
)
from shared.timestamps import to_utc

from compute.acquisition import CapacityAcquisitionService
from compute.capacity_errors import ProviderAuthorizationPendingError
from compute.context import ComputeContext
from compute.offers import (
    ComputeOffer,
    ReservationStatus,
    record_purchase_terms,
)
from compute.pool_provider import PoolProviderService
from compute.provider_machines import (
    _require_internal_pooled_unit,
    _reservation_open,
    _utc,
)
from compute.providers import (
    CapacityOwnerMutationLease,
    PooledCapacityProvider,
    ProviderCapacityPhase,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
    ResolvedComputeProvider,
)
from compute.telemetry import AGENT_HEARTBEAT_TIMEOUT_SECONDS

LOGGER = logging.getLogger(__name__)


def _retirable_platform_pool(
    pool: ComputeUnitRecord,
    provider: ResolvedComputeProvider,
    offer: ComputeOffer,
    *,
    now: datetime,
) -> bool:
    policy = provider.policy
    return (
        pool.visibility is ComputeUnitVisibility.Internal
        and not pool.stopped_machines
        and pool.platform_fleet
        and policy is not None
        and policy.platform_fleet
        and (
            not policy.can_purchase
            or not policy.accepts(offer)
            or pool.root_volume_gib != policy.root_volume_gib
            or now >= to_utc(pool.created_at) + timedelta(seconds=pool.idle_drain_timeout_seconds)
        )
    )


@dataclass(frozen=True, slots=True)
class UnitReconciliationService:
    context: ComputeContext
    capacity: CapacityAcquisitionService
    providers: PoolProviderService

    def reconcile_pooled_capacity(
        self,
        *,
        now: datetime | None = None,
    ) -> list[ComputeUnitRecord]:
        current_time = _utc(now)
        self.providers.required_capacity_owner_mutations()
        pools = self.claim_reconciliation_batch(
            ComputeReconciliationKind.Provider, now=current_time
        )
        LOGGER.info("pooled capacity reconciliation selected %d internal pool(s)", len(pools))
        reconciled: list[ComputeUnitRecord] = []
        for pool in pools:
            started = time.monotonic()
            LOGGER.info("reconciling pooled capacity for %s", pool.id)
            try:
                current = self.reconcile_unit_capacity(pool.id, now=current_time)
            except ConflictError as conflict:
                # Reported rather than characterised. Contention and a lease that
                # could not be proven on the way out are both conflicts here, and
                # naming the wrong one sends the next reader to look for a lock
                # that was never taken.
                LOGGER.info("pooled capacity for %s was not reconciled: %s", pool.name, conflict)
                continue
            finally:
                LOGGER.info(
                    "pooled capacity reconciliation for %s took %.3fs",
                    pool.id,
                    time.monotonic() - started,
                )
            if current is not None:
                reconciled.append(current)
                LOGGER.info(
                    "pooled capacity for %s: desired=%d observed=%d phase=%s%s",
                    current.name,
                    current.desired_machines,
                    current.observed_machines,
                    current.phase.value,
                    (
                        ""
                        if current.provider_state.degraded_reason is None
                        else f" degraded={current.provider_state.degraded_reason}"
                    ),
                )
        return reconciled

    def claim_reconciliation_batch(
        self, kind: ComputeReconciliationKind, *, now: datetime
    ) -> list[ComputeUnitRecord]:
        with self.context.database.session() as session:
            return ComputeUnitRepository(session).claim_reconciliation_batch(
                kind, now=now, limit=COMPUTE_RECONCILIATION_BATCH_SIZE
            )

    def reconcile_unit_capacity(
        self, unit_id: str, *, now: datetime | None = None
    ) -> ComputeUnitRecord | None:
        mutations = self.providers.required_capacity_owner_mutations()
        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get(unit_id)
        if unit is None:
            raise NotFoundError(f"compute unit not found: {unit_id}")
        _require_internal_pooled_unit(unit, unit_ref=unit_id)
        with mutations.mutation_lock(unit.capacity_owner_id):
            current_time = _utc(now)
            observed = self.reconcile_pooled_pool(
                unit.id, dispatch_fence=mutations, now=current_time
            )
            if observed is not None and observed.phase not in ENDED_UNIT_PHASES:
                self.reconcile_capacity_operations(observed, mutations=mutations, now=current_time)
                with self.context.database.session() as session:
                    return ComputeUnitRepository(session).get(unit.id)
            return observed

    def reconcile_capacity_operations(
        self, unit: ComputeUnitRecord, *, mutations: CapacityOwnerMutationLease, now: datetime
    ) -> None:
        with self.context.database.session() as session:
            operations = ComputeCapacityOperationRepository(session).expired_for_owner(
                unit.capacity_owner_id,
                created_before=now - timedelta(seconds=unit.registration_timeout_seconds),
            )
            recovery_operations = CapacityRecoveryRepository(session).active_operations(unit.id)
        for operation in operations:
            if operation.operation_id in recovery_operations:
                continue
            with mutations.dispatch_lock(unit.capacity_owner_id):
                if mutations.has_open_reservations(unit.capacity_owner_id):
                    continue
                with self.context.database.session() as session:
                    containers = ContainerRepository(session)
                    if operation.demand_container_id is not None:
                        if containers.any_with_status(
                            tuple(LIVE_CONTAINER_STATUSES),
                            container_id=operation.demand_container_id,
                        ):
                            continue
                        if containers.any_with_status(
                            (ContainerStatus.Pending,)
                        ) or ComputeProviderInstanceRepository(session).count_busy_machines(
                            unit.id
                        ):
                            # Another request may share an acquisition whose lease was lost.
                            continue
                    elif containers.any_with_status(tuple(LIVE_CONTAINER_STATUSES)):
                        # Historical acquisitions cannot be attributed safely.
                        continue
                LOGGER.info(
                    "compensating expired capacity operation %s in pool %s: "
                    "no live durable workload or reservation",
                    operation.operation_id,
                    unit.id,
                )
                self.capacity.release_acquired_capacity(
                    CapacityReleaseRequest(
                        capacity_owner_id=unit.capacity_owner_id,
                        reservation_id=operation.reservation_id,
                        operation_id=operation.operation_id,
                    )
                )

    def reconcile_pooled_pool(
        self,
        pool_id: str,
        *,
        dispatch_fence: CapacityOwnerMutationLease,
        now: datetime,
    ) -> ComputeUnitRecord | None:
        with self.context.database.session() as session:
            current = ComputeUnitRepository(session).get(pool_id)
        if current is None:
            return None
        if current.phase is ComputeUnitPhase.Deleted:
            self.providers.retire_proven_provider_pool_machines(current, now=now)
            return None
        try:
            current = self.unit_matching_its_provider(current, now=now)
            provider, offer = self.providers.resolved_internal_unit_provider(current)
            pooled = provider.pooled
            if pooled is None:
                raise UpstreamUnavailableError(
                    f"compute pool {current.name!r} provider is not pooled"
                )
            if current.phase is ComputeUnitPhase.Deleting:
                with dispatch_fence.dispatch_lock(current.capacity_owner_id):
                    snapshot = pooled.delete_unit(
                        self.providers.provider_unit_request(current, offer)
                    )
                return self.providers.machines._apply_pooled_snapshot(
                    current,
                    offer,
                    snapshot,
                    provider=pooled,
                    now=now,
                )
            if (
                provider.policy is not None
                and not provider.policy.can_purchase
                and (current.desired_machines or current.observed_machines)
            ):
                with dispatch_fence.dispatch_lock(current.capacity_owner_id):
                    pooled.ensure_unit(self.providers.provider_unit_request(current, offer))
            current = self.reclaim_pooled_bootstrap_failures(
                current,
                pooled=pooled,
                offer=offer,
                now=now,
            )
            with self.context.database.session() as session:
                records = ComputeProviderInstanceRepository(session).list_for_pool(
                    current.id,
                    statuses=(
                        ReservationStatus.Terminating.value,
                        ReservationStatus.Resuming.value,
                    ),
                )
            for record in records:
                if (
                    record.status != ReservationStatus.Terminating.value
                    or record.instance_id is None
                    or record.missing_since is not None
                ):
                    continue
                with dispatch_fence.dispatch_lock(current.capacity_owner_id):
                    pooled.release_machine(
                        self.providers.provider_unit_request(current, offer), record.instance_id
                    )
            resumed = [
                record.instance_id
                for record in records
                if record.status == ReservationStatus.Resuming.value
                and record.resume_authorized_at is not None
                and record.instance_id is not None
                and record.missing_since is None
            ]
            for instance_id in resumed:
                # One instance the provider no longer owns, such as a reclaimed
                # Spot machine, must not stop the rest of the pass.
                try:
                    snapshot = pooled.complete_machine_preparation(
                        self.providers.provider_unit_request(current, offer),
                        instance_id,
                        sleep_request=None,
                    )
                except Exception:
                    LOGGER.warning(
                        "finishing the resume of %s in %s failed",
                        instance_id,
                        current.id,
                        exc_info=True,
                    )
                    continue
                current = self.providers.machines._apply_pooled_snapshot(
                    current, offer, snapshot, provider=pooled, now=now
                )
            request = self.providers.provider_unit_request(current, offer)
            if request.desired_machines == 0 and request.stopped_machines == 0:
                with dispatch_fence.dispatch_lock(current.capacity_owner_id):
                    snapshot = pooled.describe_unit(request)
                    if (
                        snapshot.desired_machines
                        or snapshot.observed_machines
                        or snapshot.instances
                    ):
                        snapshot = pooled.set_unit_capacity(
                            request,
                            desired_machines=0,
                            max_machines=request.max_machines,
                        )
                    current = self.providers.machines._apply_pooled_snapshot(
                        current,
                        offer,
                        snapshot,
                        provider=pooled,
                        now=now,
                    )
                    return self.retire_empty_platform_pool(
                        current,
                        provider=provider,
                        offer=offer,
                        snapshot=snapshot,
                        mutations=dispatch_fence,
                        now=now,
                    )
            offer = self.providers.available_unit_offer(provider, current)
            rejection = self.providers.pooled_offer_rejection(
                provider, offer, preemptible=current.worker_preemptible, now=now
            )
            placement_allows = rejection is None
            degraded = current.provider_state.degraded_reason is not None or not placement_allows
            if not placement_allows:
                LOGGER.warning(
                    "purchase policy prevents restoring pool %s: %s", current.id, rejection
                )
            if not degraded:
                with self.context.database.session() as session:
                    current = ComputeUnitRepository(session).upsert(
                        record_purchase_terms(current, offer)
                    )
            request = self.providers.provider_unit_request(current, offer)
            if degraded:
                request = request.model_copy(update={"purchases_enabled": False})
            snapshot = self.ensure_reconciled_pool(
                current,
                pooled=pooled,
                request=request,
                dispatch_fence=dispatch_fence,
            )
            return self.providers.machines._apply_pooled_snapshot(
                current,
                offer,
                snapshot,
                provider=pooled,
                now=now,
            )
        except ProviderAuthorizationPendingError as exc:
            LOGGER.info("pooled capacity reconciliation deferred for %s: %s", current.id, exc)
            return current
        except StaleProviderStateError:
            LOGGER.debug("provider observation for %s was superseded", current.id)
            return current
        except Exception:
            LOGGER.exception(
                "pooled provider reconciliation failed for pool %s (%s)",
                current.name,
                current.id,
            )
            return self.providers.mark_pooled_capacity_degraded(
                current,
                preserve_deleting=True,
            )

    def retire_empty_platform_pool(
        self,
        pool: ComputeUnitRecord,
        *,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        snapshot: ProviderUnitSnapshot,
        mutations: CapacityOwnerMutationLease,
        now: datetime,
    ) -> ComputeUnitRecord:
        if not _retirable_platform_pool(pool, provider, offer, now=now):
            return pool
        if (
            snapshot.phase not in {ProviderCapacityPhase.Ready, ProviderCapacityPhase.Deleted}
            or snapshot.desired_machines
            or snapshot.observed_machines
            or snapshot.instances
            or mutations.has_open_reservations(pool.capacity_owner_id)
        ):
            return pool
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            units.lock_platform_capacity()
            current = units.get(pool.id, for_update=True)
            if current is None:
                raise ConflictError("compute pool disappeared during retirement")
            if (
                current.generation != pool.generation
                or current.phase in ENDED_UNIT_PHASES
                or not _retirable_platform_pool(current, provider, offer, now=now)
                or current.desired_machines
                or current.observed_machines
                or current.min_machines
                or current.min_free_cpu_millicores
                or current.min_free_memory_mib
                or current.min_free_gpu_count
                or current.replacement_machine_id
                or current.replacement_template_version
                or current.maintenance_active
                or CapacityRecoveryRepository(session).unit_has_active_recovery(current.id)
                or ComputeCapacityOperationRepository(session).list_open_for_owner(
                    current.capacity_owner_id
                )
                or ComputeProviderInstanceRepository(session).count_busy_machines(current.id)
            ):
                return current
            records = ComputeProviderInstanceRepository(session).list_for_pool(current.id)
            if any(
                _reservation_open(record.status)
                or (
                    (record.instance_id is not None or record.storage_volume_ids)
                    and record.provider_storage_destroyed_at is None
                )
                for record in records
            ):
                return current
            retiring = units.upsert(
                current.model_copy(
                    update={
                        "generation": current.generation + 1,
                        "phase": ComputeUnitPhase.Deleting,
                        "status": ComputeUnitPhase.Deleting.value,
                    }
                )
            )
        pooled = provider.pooled
        if pooled is None:
            raise UpstreamUnavailableError("compute pool provider is not pooled")
        LOGGER.info("retiring empty platform pool %s", retiring.id)
        return self.providers.machines._apply_pooled_snapshot(
            retiring,
            offer,
            pooled.delete_unit(self.providers.provider_unit_request(retiring, offer)),
            provider=pooled,
            now=now,
        )

    @staticmethod
    def ensure_reconciled_pool(
        pool: ComputeUnitRecord,
        *,
        pooled: PooledCapacityProvider,
        request: ProviderUnitRequest,
        dispatch_fence: CapacityOwnerMutationLease,
    ) -> ProviderUnitSnapshot:
        if pool.observed_machines <= pool.desired_machines:
            snapshot = pooled.ensure_unit(request)
        else:
            with dispatch_fence.dispatch_lock(pool.capacity_owner_id):
                snapshot = pooled.ensure_unit(request)
        if snapshot.observed_machines < snapshot.desired_machines:
            return pooled.describe_unit(request)
        return snapshot

    def unit_matching_its_provider(
        self,
        unit: ComputeUnitRecord,
        *,
        now: datetime,
    ) -> ComputeUnitRecord:
        """Reconcile the pool and ownership from its authoritative provider policy."""
        if self.providers.provider_resolver is None:
            raise UpstreamUnavailableError("compute provider resolver is unavailable")
        provider = self.providers.provider_resolver.resolve(unit.workspace_id, unit.provider_ref)
        policy = provider.policy
        if policy is None:
            raise UpstreamUnavailableError("compute provider policy is unavailable")
        if unit.placement == policy.placement and unit.platform_fleet == policy.platform_fleet:
            return unit
        with self.context.database.session() as session:
            updated = ComputeUnitRepository(session).upsert(
                unit.model_copy(
                    update={
                        "placement": policy.placement,
                        "platform_fleet": policy.platform_fleet,
                        "updated_at": now,
                    }
                )
            )
            # The placement is stamped on every row the unit produced, and each
            # lookup compares it, so the rows move with the unit or the unit's
            # machines stop being found under it.
            ComputeMachineEnrollmentRepository(session).move_placement_for_unit(
                unit.workspace_id, unit.capacity_owner_id, policy.placement
            )
            MachineRepository(session).move_placement_for_capacity_owner(
                unit.workspace_id, unit.capacity_owner_id, policy.placement
            )
        LOGGER.info(
            "compute unit %s follows its provider: placement %s -> %s, platform fleet %s -> %s",
            unit.name,
            unit.placement,
            updated.placement,
            unit.platform_fleet,
            updated.platform_fleet,
        )
        return updated

    def pool_agents_reachable(
        self,
        session: DatabaseSession,
        pool: ComputeUnitRecord,
        records: list[ComputeProviderInstanceRecord],
        *,
        now: datetime,
    ) -> bool:
        """Suspend reclaim when all agents in a populated pool are unreachable.
        A singleton must remain reclaimable after its own failure."""

        machine_ids = [
            record.machine_id
            for record in records
            if record.machine_id is not None
            and record.status
            not in {
                ReservationStatus.Stopped.value,
                ReservationStatus.Stopping.value,
                ReservationStatus.Preparing.value,
            }
        ]
        if len(machine_ids) < 2:
            return True
        enrollments = ComputeMachineEnrollmentRepository(session)
        cutoff = now - timedelta(seconds=AGENT_HEARTBEAT_TIMEOUT_SECONDS)
        active = 0
        for machine_id in machine_ids:
            enrollment = enrollments.by_machine(pool.workspace_id, machine_id)
            if enrollment is None:
                continue
            if enrollment.status is not ComputeMachineEnrollmentStatus.Active:
                continue
            active += 1
            if (
                enrollment.last_heartbeat_at is not None
                and to_utc(enrollment.last_heartbeat_at) >= cutoff
            ):
                return True
        if active < 2:
            return True
        LOGGER.warning(
            "pooled capacity reclaim deferred for %s: no agent reports observed",
            pool.name,
        )
        return False

    def reclaim_pooled_bootstrap_failures(
        self,
        pool: ComputeUnitRecord,
        *,
        pooled: PooledCapacityProvider,
        offer: ComputeOffer,
        now: datetime,
    ) -> ComputeUnitRecord:
        """Reclaim missed bootstrap deadlines. Terminal deletion still requires
        proof of instance and storage absence from the provider snapshot."""
        hooks = self.providers.machines.scheduler_hooks
        if hooks is None:
            # Reclaim requires worker observations to establish a bootstrap failure.
            LOGGER.warning(
                "pooled capacity reclaim declined for %s: no scheduler worker state is configured",
                pool.name,
            )
            return pool
        observing_since = hooks.agent_intake_observing_since()
        if observing_since is None:
            # Missing intake cannot establish that a machine failed to report.
            LOGGER.warning(
                "pooled capacity reclaim declined for %s: no agent heartbeat intake is registered",
                pool.name,
            )
            return pool
        to_reclaim: list[tuple[ComputeProviderInstanceRecord, MachineBootstrapFailureReason]] = []
        with self.context.database.session() as session:
            records = ComputeProviderInstanceRepository(session).list_for_pool(
                pool.id,
                excluded_statuses=(
                    ReservationStatus.Deleted.value,
                    ReservationStatus.Failed.value,
                    ReservationStatus.Terminating.value,
                ),
            )
            pool_reachable = self.pool_agents_reachable(session, pool, records, now=now)
            containers = ContainerRepository(session)
            for record in records:
                observation = self.providers.machines._observe_provider_service_state(
                    session,
                    pool,
                    record,
                    pool_reachable=pool_reachable,
                )
                observed = self.providers.machines._record_service_observation(
                    session,
                    record,
                    observation,
                    now=now,
                )
                failure = self.providers.machines._provider_bootstrap_failure_to_reclaim(
                    session,
                    pool,
                    observed,
                    now=now,
                    observing_since=observing_since,
                    live_containers=(
                        containers.count_live_for_machine(observed.machine_id)
                        if observed.machine_id is not None
                        else 0
                    ),
                )
                if failure is not None:
                    to_reclaim.append((observed, failure))
        if not to_reclaim:
            return pool
        current = pool
        attempts_exhausted = any(
            record.launch_attempt - current.provider_state.launch_attempt_baseline
            >= self.providers.reclaim.max_launch_attempts_for(record.provider)
            for record, _failure in to_reclaim
        )
        if attempts_exhausted:
            degraded = self.providers.mark_pooled_capacity_degraded(
                current, reason="bootstrap_launch_attempts_exhausted", now=now
            )
            if degraded is not None:
                current = degraded
        for record, failure in to_reclaim:
            with self.context.database.session() as session:
                self.providers.machines._terminate_provider_record(
                    session,
                    record,
                    clients={},
                    reason="bootstrap_deadline_exceeded",
                    message=(
                        "machine did not produce an available worker before "
                        "the bootstrap phase deadline"
                    ),
                    bootstrap_failure_reason=failure,
                    bootstrap_observed_at=now,
                )
            if record.instance_id is not None:
                snapshot = pooled.release_machine(
                    self.providers.provider_unit_request(current, offer),
                    record.instance_id,
                )
                current = self.providers.machines._apply_pooled_snapshot(
                    current,
                    offer,
                    snapshot,
                    provider=pooled,
                    now=now,
                )
            LOGGER.warning(
                "reclaimed pooled provider machine that did not become ready",
                extra={
                    "provider": record.provider,
                    "placement": current.placement.key,
                    "unit": current.name,
                    "machine_id": record.machine_id,
                    "provider_instance_id": record.instance_id or record.id,
                    "launch_attempt": record.launch_attempt,
                },
            )
        return current
