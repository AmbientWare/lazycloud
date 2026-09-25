from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from database.repositories.capacity_recovery import (
    CapacityRecoveryRecord,
    CapacityRecoveryRepository,
)
from database.repositories.compute import (
    ComputeCapacityOperationRecord,
    ComputeCapacityOperationRepository,
    ComputeMachineEnrollmentRecord,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import ContainerRepository
from database.types import DatabaseSession
from shared.capacity import (
    CapacityAcquisitionRequest,
    CapacityAcquisitionShape,
    CapacityAcquisitionStatus,
    CapacityFailureCode,
    CapacityFulfillmentRequest,
    CapacityOperationStatus,
    CapacityReleaseRequest,
    capacity_failure_message,
)
from shared.compute_enrollment import AgentCapacityState
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeResourceRequirements,
    ComputeUnitPhase,
    ComputeUnitRecord,
)
from shared.errors import UpstreamUnavailableError
from shared.timestamps import utc_now

from compute.agent_control import machine_serves_workloads
from compute.capacity_errors import CapacityReservationLockContendedError
from compute.offers import (
    ComputeOffer,
    OfferRequest,
    ReservationStatus,
    cooling_regions,
    filter_offers,
    offer_selection_key,
)
from compute.providers import ResolvedComputeProvider

if TYPE_CHECKING:
    from compute.service import ComputeService

LOGGER = logging.getLogger(__name__)
CAPACITY_WAKE_SCOPE = "scheduler-capacity-acquisition"
"""Wakes the scheduler's acquisition loop: a machine is threatened or a request needs one."""
MAX_RECOVERY_ATTEMPTS = 3


def record_capacity_risk(
    session: DatabaseSession, enrollment: ComputeMachineEnrollmentRecord
) -> None:
    if (
        enrollment.capacity_state is AgentCapacityState.Available
        or enrollment.capacity_observed_at is None
    ):
        return
    unit = ComputeUnitRepository(session).get(enrollment.capacity_owner_id)
    if unit is None or unit.capacity_mode is not ComputeCapacityMode.Pooled:
        return
    machine = ComputeProviderInstanceRepository(session).get_by_machine(enrollment.machine_id)
    if machine is None or machine.pool_id != unit.id:
        return
    if machine.status in {
        ReservationStatus.Preparing.value,
        ReservationStatus.Stopping.value,
        ReservationStatus.Stopped.value,
    }:
        # A reserve holds no work and no running target. The provider replaces an
        # interrupted one; recovering it would lower the pool's running target.
        return
    CapacityRecoveryRepository(session).record_signal(
        workspace_id=unit.workspace_id,
        source_unit_id=unit.id,
        source_machine_id=enrollment.machine_id,
        observed_at=enrollment.capacity_observed_at,
        deadline=enrollment.capacity_notice_at,
        now=utc_now(),
    )


@dataclass(slots=True)
class CapacityRecoveryService:
    compute: ComputeService

    def reconcile(self, *, now: datetime, limit: int = 16) -> None:
        with self.compute.context.database.session() as session:
            identities = CapacityRecoveryRepository(session).claim_due(now=now, limit=limit)
        reconciled_targets: set[str] = set()
        for identity in identities:
            try:
                with self.compute._required_capacity_owner_mutations().mutation_lock(identity):
                    self._reconcile(identity, now=now, reconciled_targets=reconciled_targets)
            except CapacityReservationLockContendedError:
                continue
            except Exception:
                LOGGER.exception("capacity recovery failed for %s", identity)

    def _save(self, record: CapacityRecoveryRecord, *, now: datetime) -> None:
        with self.compute.context.database.session() as session:
            CapacityRecoveryRepository(session).save(
                record.model_copy(
                    update={
                        "next_action_at": now + timedelta(seconds=5),
                    }
                )
            )

    def _reconcile(self, identity: str, *, now: datetime, reconciled_targets: set[str]) -> None:
        with self.compute.context.database.session() as session:
            record = CapacityRecoveryRepository(session).get(identity)
            if record is None or record.completed_at is not None:
                return
            source = ComputeUnitRepository(session).get(record.source_unit_id)
        if source is None:
            raise UpstreamUnavailableError("capacity recovery source is missing")
        source_provider, _ = self.compute._resolved_internal_unit_provider(source)
        if (
            source.phase in {ComputeUnitPhase.Deleting, ComputeUnitPhase.Deleted}
            or source_provider.policy is None
            or not source_provider.policy.can_purchase
        ):
            self._finish_without_replacement(record, reason="capacity owner retired", now=now)
            return
        if not record.source_adjusted:
            source, record = self._adjust_source(source, record, now=now)
        # Persisted reduction must reach the provider before it can replenish the old market.
        if not record.source_applied:
            with self.compute._required_capacity_owner_mutations().mutation_lock(source.id):
                with self.compute.context.database.session() as session:
                    current = ComputeUnitRepository(session).get(source.id)
                if current is None:
                    raise UpstreamUnavailableError("capacity recovery source is missing")
                source = current
                provider, offer = self.compute._resolved_internal_unit_provider(source)
                if provider.pooled is None:
                    raise UpstreamUnavailableError("capacity recovery source is not pooled")
                request = self.compute._provider_unit_request(source, offer)
                if source.phase not in {ComputeUnitPhase.Deleted, ComputeUnitPhase.Deleting}:
                    snapshot = provider.pooled.set_unit_capacity(
                        request,
                        desired_machines=request.desired_machines,
                        max_machines=request.max_machines,
                    )
                    self.compute.provider_machines._apply_pooled_snapshot(
                        source,
                        offer,
                        snapshot,
                        provider=provider.pooled,
                        now=now,
                    )
                record = record.model_copy(update={"source_applied": True})
                self._save(record, now=now)
        if record.replacement_machine_id is None and (
            now >= record.observed_at + timedelta(seconds=source.registration_timeout_seconds)
            or (record.target_unit_id is None and record.attempt >= MAX_RECOVERY_ATTEMPTS)
        ):
            self._finish_without_replacement(
                record, reason="replacement acquisition exhausted", now=now
            )
            return
        if record.replacement_machine_id:
            self._settle(source, record, now=now)
            return
        if record.target_unit_id is None:
            selected = self._select_target(source, now=now)
            if selected is None:
                self._save(
                    record.model_copy(update={"reason": "no eligible replacement market"}), now=now
                )
                return
            provider, offer = selected
            target_id = self.compute.pooled_offer_owner_id(provider, offer)
            with self.compute._required_capacity_owner_mutations().mutation_lock(target_id):
                target = self.compute.prepare_pooled_offer(
                    provider=provider,
                    offer=offer,
                    requirements=ComputeResourceRequirements(preemptible=source.worker_preemptible),
                )
            record = record.model_copy(
                update={
                    "target_unit_id": target.id,
                    "attempt": record.attempt + 1,
                    "operation_id": str(uuid5(NAMESPACE_URL, f"{record.id}:{record.attempt + 1}")),
                    "reason": "replacement acquisition admitted",
                }
            )
            self._save(record, now=now)
        self._acquire(record, now=now, reconciled_targets=reconciled_targets)

    def _adjust_source(
        self,
        source: ComputeUnitRecord,
        record: CapacityRecoveryRecord,
        *,
        now: datetime,
    ) -> tuple[ComputeUnitRecord, CapacityRecoveryRecord]:
        with (
            self.compute._required_capacity_owner_mutations().mutation_lock(source.id),
            self.compute.context.database.session() as session,
        ):
            units = ComputeUnitRepository(session)
            if source.platform_fleet:
                units.lock_platform_capacity()
            else:
                units.lock_capacity_workspace(source.workspace_id)
            current = units.get(source.id, for_update=True)
            recovery = CapacityRecoveryRepository(session).get(record.id, for_update=True)
            if current is None or recovery is None:
                raise UpstreamUnavailableError("capacity recovery ownership disappeared")
            if recovery.source_adjusted:
                return current, recovery
            paired_surge = current.replacement_machine_id == record.source_machine_id
            desired = max(current.desired_machines - (0 if paired_surge else 1), 0)
            if paired_surge and desired > 0:
                operation_id = str(uuid5(NAMESPACE_URL, f"{record.id}:1"))
                ComputeCapacityOperationRepository(session).upsert(
                    ComputeCapacityOperationRecord(
                        id=operation_id,
                        workspace_id=source.workspace_id,
                        pool_id=source.id,
                        capacity_owner_id=source.id,
                        reservation_id=operation_id,
                        operation_id=operation_id,
                        desired_unit=desired,
                        status=CapacityOperationStatus.ExistingPending,
                        shape=CapacityAcquisitionShape(
                            cpu_millicores=source.worker_cpu_millicores,
                            memory_mib=source.worker_memory_mib,
                            gpu_type=source.worker_gpu_type,
                            gpu_count=source.worker_gpu_count,
                            runtime=source.worker_runtimes[0],
                            preemptible=source.worker_preemptible,
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                )
                recovery = recovery.model_copy(
                    update={
                        "target_unit_id": source.id,
                        "operation_id": operation_id,
                        "attempt": 1,
                    }
                )
            current = units.upsert(
                current.model_copy(
                    update={
                        "desired_machines": desired,
                        "min_machines": min(current.min_machines, desired),
                        "initial_machines": min(current.initial_machines, desired),
                        "replacement_machine_id": (
                            ""
                            if current.replacement_machine_id == record.source_machine_id
                            else current.replacement_machine_id
                        ),
                        "replacement_template_version": (
                            ""
                            if current.replacement_machine_id == record.source_machine_id
                            else current.replacement_template_version
                        ),
                        "generation": current.generation + 1,
                        "provider_state": current.provider_state.model_copy(
                            update={
                                "degraded_reason": "provider_interruption",
                                "degraded_at": now,
                            }
                        ),
                    }
                )
            )
            recovery = recovery.model_copy(update={"source_adjusted": True})
            CapacityRecoveryRepository(session).save(recovery)
            return current, recovery

    def _select_target(
        self,
        source: ComputeUnitRecord,
        *,
        now: datetime,
    ) -> tuple[ResolvedComputeProvider, ComputeOffer] | None:
        resolver = self.compute.provider_resolver
        if resolver is None:
            raise UpstreamUnavailableError("capacity recovery provider resolver is unavailable")
        source_provider, source_offer = self.compute._resolved_internal_unit_provider(source)
        providers = (
            tuple(resolver.list_platform_providers())
            if source.platform_fleet
            else (source_provider,)
        )
        # A platform machine serves any region's work, so its replacement may
        # come from another region when the source's is short. A connected
        # account's capacity stays in the region its owner placed it.
        request = OfferRequest(
            regions=[] if source.platform_fleet else [source.region],
            preemptible=source.worker_preemptible,
            min_storage_mb=source.root_volume_gib * 1024,
            architecture=source_offer.architecture,
            runtime=source_offer.runtime,
            gpu=[source.worker_gpu_type] if source.worker_gpu_type else [],
            min_gpu_count=source.worker_gpu_count,
            nodes=1,
        )
        candidates: list[tuple[ResolvedComputeProvider, ComputeOffer]] = []
        for provider in providers:
            if (
                provider.pooled is None
                or provider.policy is None
                or not provider.policy.can_purchase
            ):
                continue
            for offer in filter_offers(
                list(provider.pooled.list_offers(root_volume_gib=source.root_volume_gib)), request
            ):
                # Worker resources already include the capacity reserved for its runtime.
                if (
                    offer.cpu_millicores < source.worker_cpu_millicores
                    or offer.memory_mb < source.worker_memory_mib
                    or offer.preemptible is not source.worker_preemptible
                ):
                    continue
                if offer.market == source_offer.market:
                    continue
                if self.compute.pooled_offer_rejection(
                    provider, offer, preemptible=source.worker_preemptible, now=now
                ):
                    continue
                candidates.append((provider, offer))
        identities = {
            (
                provider.policy.workspace_id,
                provider.ref,
                offer.region,
                offer.capability_key,
                provider.policy.root_volume_gib,
            ): (provider, offer)
            for provider, offer in candidates
            if provider.policy is not None
        }
        with self.compute.context.database.session() as session:
            states = ComputeUnitRepository(session).offer_states(tuple(identities))
        short_regions = cooling_regions(
            (
                (
                    identities[identity][1].market.cloud,
                    identity[2],
                    state.provider_state.last_capacity_failure_at,
                )
                for identity, state in states.items()
            ),
            now=now,
        )
        eligible: list[tuple[ResolvedComputeProvider, ComputeOffer]] = []
        concentration: dict[tuple[str, str], int] = {}
        for identity, candidate in identities.items():
            unit = states.get(identity)
            if unit is not None and (
                unit.phase is ComputeUnitPhase.Deleting
                or (
                    unit.provider_state.degraded_reason is not None
                    and not self.compute._failed_market_retry_ready(unit, now=now)
                )
            ):
                continue
            eligible.append(candidate)
            concentration[(candidate[0].ref, candidate[1].id)] = (
                max(unit.desired_machines, unit.observed_machines) if unit else 0
            )
        return min(
            eligible,
            key=lambda item: (
                item[1].availability_zone == source_offer.availability_zone,
                (item[1].market.cloud, item[1].region) in short_regions,
                item[1].region != source.region,
                item[0].policy.region_rank(item[1].region) if item[0].policy is not None else 0,
                concentration[(item[0].ref, item[1].id)],
                offer_selection_key(item[1], request),
            ),
            default=None,
        )

    def _acquire(
        self, record: CapacityRecoveryRecord, *, now: datetime, reconciled_targets: set[str]
    ) -> None:
        assert record.target_unit_id is not None and record.operation_id is not None
        with self.compute._required_capacity_owner_mutations().mutation_lock(record.target_unit_id):
            with self.compute.context.database.session() as session:
                target = ComputeUnitRepository(session).get(record.target_unit_id)
            if target is None:
                raise UpstreamUnavailableError("capacity recovery target is missing")
            if target.id not in reconciled_targets:
                self.compute.reconcile_unit_capacity(target.id, now=now)
                reconciled_targets.add(target.id)
            with self.compute.context.database.session() as session:
                current_target = ComputeUnitRepository(session).get(target.id)
                if current_target is None:
                    raise UpstreamUnavailableError("capacity recovery target is missing")
                target = current_target
                operation = ComputeCapacityOperationRepository(session).get(
                    target.id, record.operation_id
                )
                machines = ComputeProviderInstanceRepository(session).list_for_pool(
                    target.id, statuses=(ReservationStatus.Active.value,)
                )
            if operation is None and (
                target.provider_state.degraded_reason is not None
                or target.phase in {ComputeUnitPhase.Deleting, ComputeUnitPhase.Deleted}
            ):
                self._save(
                    record.model_copy(
                        update={
                            "target_unit_id": None,
                            "operation_id": None,
                            "reason": "replacement market is unavailable",
                        }
                    ),
                    now=now,
                )
                return
            hooks = self.compute.scheduler_hooks
            if hooks is None:
                raise UpstreamUnavailableError("capacity recovery requires worker readiness")
            if operation is not None and operation.status is CapacityOperationStatus.Fulfilled:
                self._save(
                    record.model_copy(
                        update={
                            "replacement_machine_id": operation.target_machine_id,
                            "reason": "replacement accepts work",
                        }
                    ),
                    now=now,
                )
                return
            if operation is not None:
                for machine in machines:
                    if machine.machine_id is None or machine.created_at < record.observed_at:
                        continue
                    if not self._replacement_ready(target.workspace_id, machine.machine_id):
                        continue
                    with self.compute.context.database.session() as session:
                        if CapacityRecoveryRepository(session).machine_is_replacement(
                            machine.machine_id
                        ):
                            continue
                    status = self.compute.fulfill_acquired_capacity(
                        CapacityFulfillmentRequest(
                            capacity_owner_id=target.id,
                            operation_id=record.operation_id,
                            reservation_id=record.operation_id,
                            machine_id=machine.machine_id,
                        )
                    )
                    if status is CapacityOperationStatus.Fulfilled:
                        self._save(
                            record.model_copy(
                                update={
                                    "replacement_machine_id": machine.machine_id,
                                    "reason": "replacement accepts work",
                                }
                            ),
                            now=now,
                        )
                        return
            if operation is not None and (
                operation.status
                in {
                    CapacityOperationStatus.Rejected,
                    CapacityOperationStatus.Released,
                    CapacityOperationStatus.Releasing,
                    CapacityOperationStatus.Unsupported,
                }
                or now
                >= operation.created_at + timedelta(seconds=target.registration_timeout_seconds)
            ):
                failure_code = operation.failure_code
                self.compute.release_acquired_capacity(
                    CapacityReleaseRequest(
                        capacity_owner_id=target.id,
                        operation_id=record.operation_id,
                        reservation_id=record.operation_id,
                    )
                )
                with self.compute.context.database.session() as session:
                    operation = ComputeCapacityOperationRepository(session).get(
                        target.id, record.operation_id
                    )
                if operation is not None and operation.status in {
                    CapacityOperationStatus.Released,
                    CapacityOperationStatus.Unsupported,
                }:
                    record = record.model_copy(
                        update={"target_unit_id": None, "operation_id": None}
                    )
                    if failure_code is not None and failure_code in {
                        CapacityFailureCode.ProviderQuotaExceeded,
                        CapacityFailureCode.JoinAuthorityUnusable,
                        CapacityFailureCode.ProviderUnavailable,
                    }:
                        self._save(
                            record.model_copy(
                                update={
                                    "completed_at": now,
                                    "reason": capacity_failure_message(failure_code),
                                }
                            ),
                            now=now,
                        )
                        return
                self._save(
                    record.model_copy(update={"reason": "replacement acquisition failed"}), now=now
                )
                return
            acquisition = CapacityAcquisitionRequest(
                capacity_owner_id=target.id,
                reservation_id=record.operation_id,
                operation_id=record.operation_id,
                shape=CapacityAcquisitionShape(
                    cpu_millicores=target.worker_cpu_millicores,
                    memory_mib=target.worker_memory_mib,
                    gpu_type=target.worker_gpu_type,
                    gpu_count=target.worker_gpu_count,
                    runtime=target.worker_runtimes[0],
                    preemptible=target.worker_preemptible,
                ),
            )
            with self.compute.context.database.session() as session:
                other_claims = CapacityRecoveryRepository(session).other_target_claims(
                    target.id, record.id, observed_at=record.observed_at
                )
            earlier_machines = sum(machine.created_at < record.observed_at for machine in machines)
            if operation is None and target.desired_machines > earlier_machines + other_claims:
                result = self.compute._acquire_pooled_capacity(
                    target,
                    acquisition,
                    desired_unit=target.desired_machines,
                )
            else:
                result = self.compute.ensure_capacity(acquisition)
            if result.status is CapacityAcquisitionStatus.AtLimit:
                reason = "fleet capacity limit prevents replacement"
            else:
                reason = result.reason or "replacement is starting"
            self._save(record.model_copy(update={"reason": reason}), now=now)

    def _finish_without_replacement(
        self, record: CapacityRecoveryRecord, *, reason: str, now: datetime
    ) -> None:
        if record.target_unit_id is not None and record.operation_id is not None:
            with self.compute._required_capacity_owner_mutations().mutation_lock(
                record.target_unit_id
            ):
                self.compute.release_acquired_capacity(
                    CapacityReleaseRequest(
                        capacity_owner_id=record.target_unit_id,
                        operation_id=record.operation_id,
                        reservation_id=record.operation_id,
                    )
                )
                with self.compute.context.database.session() as session:
                    operation = ComputeCapacityOperationRepository(session).get(
                        record.target_unit_id, record.operation_id
                    )
                if operation is not None and not operation.status.terminal:
                    self._save(record.model_copy(update={"reason": reason}), now=now)
                    return
        self._save(record.model_copy(update={"completed_at": now, "reason": reason}), now=now)

    def _settle(
        self, source: ComputeUnitRecord, record: CapacityRecoveryRecord, *, now: datetime
    ) -> None:
        if record.replacement_machine_id is None or record.target_unit_id is None:
            raise UpstreamUnavailableError("capacity recovery replacement is missing")
        with self.compute.context.database.session() as session:
            target = ComputeUnitRepository(session).get(record.target_unit_id)
        if target is None or not self._replacement_ready(
            target.workspace_id, record.replacement_machine_id
        ):
            if now >= record.observed_at + timedelta(seconds=source.registration_timeout_seconds):
                self._finish_without_replacement(
                    record, reason="replacement lost readiness before recovery completed", now=now
                )
                return
            self._save(
                record.model_copy(update={"reason": "replacement is not accepting work"}), now=now
            )
            return
        with self.compute.context.database.session() as session:
            machine = ComputeProviderInstanceRepository(session).get_by_machine(
                record.source_machine_id
            )
            busy = ContainerRepository(session).count_live_for_machine(record.source_machine_id) > 0
        if machine is None or (
            machine.status == ReservationStatus.Deleted.value
            and machine.provider_storage_destroyed_at is not None
        ):
            self._save(
                record.model_copy(update={"completed_at": now, "reason": "replacement complete"}),
                now=now,
            )
        elif not busy and machine.status not in {
            ReservationStatus.Terminating.value,
            ReservationStatus.Deleted.value,
        }:
            self.compute.drain_internal_unit_machine(
                source.workspace_id,
                record.source_machine_id,
                reason="replacement capacity accepts work",
                now=now,
            )
            self.compute.release_internal_unit_machine(
                source.workspace_id, source.id, record.source_machine_id
            )

    def _replacement_ready(self, workspace_id: str, machine_id: str) -> bool:
        hooks = self.compute.scheduler_hooks
        if hooks is None:
            raise UpstreamUnavailableError("capacity recovery requires worker readiness")
        with self.compute.context.database.session() as session:
            enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                workspace_id, machine_id
            )
            instance = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
        return (
            instance is not None
            and instance.status == ReservationStatus.Active.value
            and instance.missing_since is None
            and machine_serves_workloads(enrollment, machine_id=machine_id, worker_state=hooks)
        )
