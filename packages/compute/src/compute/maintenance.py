from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.repositories.capacity_maintenance import CapacityMaintenanceRepository
from database.repositories.capacity_recovery import CapacityRecoveryRepository
from database.repositories.compute import (
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
)
from database.repositories.worker_releases import WorkerReleaseRepository
from database.types import DatabaseSession
from shared.capacity_maintenance import (
    CapacityMaintenanceKind,
    CapacityMaintenancePhase,
    CapacityMaintenanceRecord,
)
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputeMachineEnrollmentStatus,
)
from shared.compute_fleet import MachineLifecycle
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitRecord,
)
from shared.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
)
from shared.gpu import GPU_ANY
from shared.releases import ActiveRelease
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now

from compute.context import ComputeContext
from compute.fleet_reserves import (
    machine_capacity,
    unit_reserve_market,
)
from compute.fleet_resources import Capacity
from compute.machine_lifecycle import (
    machine_lifecycle_allowed,
    write_machine_lifecycle,
)
from compute.maintenance_policy import MaintenanceBudget, MaintenanceCandidate, plan_maintenance
from compute.offers import ReservationStatus
from compute.pool_provider import PoolProviderService
from compute.provider_machines import (
    _require_internal_pooled_unit,
    _utc,
)


def _provider_machine_can_be_replaced(record: ComputeProviderInstanceRecord) -> bool:
    # Providers may still report a retired instance while its termination runs.
    return (
        bool(record.instance_id)
        and record.status in {ReservationStatus.Pending.value, ReservationStatus.Active.value}
        and record.missing_since is None
    )


def release_replacement(
    source: SchedulerWorkerRecord,
    workers: list[SchedulerWorkerRecord],
    release: ActiveRelease,
    *,
    now: datetime,
) -> SchedulerWorkerRecord | None:
    """A current worker in the same pool with room for the source's allocations."""
    for candidate in workers:
        if (
            candidate.machine_id == source.machine_id
            or candidate.capacity_owner_id != source.capacity_owner_id
            or candidate.placement != source.placement
            or candidate.owner_user_id != source.owner_user_id
            or candidate.gpu_type != source.gpu_type
            or candidate.disk_storage != source.disk_storage
            or (
                source.total_disk_volumes > source.free_disk_volumes
                and source.availability_zone
                and candidate.availability_zone != source.availability_zone
            )
            or not set(source.runtime_classes).issubset(candidate.runtime_classes)
            or candidate.request_intake_status(at=now) is not SchedulerWorkerStatus.Available
            or not release.admits(candidate.runtime_image, candidate.agent_binary_sha256)
        ):
            continue
        if all(
            free >= max(total - remaining, 0)
            for free, total, remaining in (
                (
                    candidate.free_cpu_millicores,
                    source.total_cpu_millicores,
                    source.free_cpu_millicores,
                ),
                (candidate.free_memory_mib, source.total_memory_mib, source.free_memory_mib),
                (candidate.free_gpu_count, source.total_gpu_count, source.free_gpu_count),
                (candidate.free_disk_bytes, source.total_disk_bytes, source.free_disk_bytes),
                (candidate.free_disk_volumes, source.total_disk_volumes, source.free_disk_volumes),
            )
        ):
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class CapacityMaintenanceService:
    context: ComputeContext
    providers: PoolProviderService

    def begin_internal_unit_replacement(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
        *,
        template_version: str = "",
    ) -> ComputeUnitRecord:
        """Durably pair one retiring machine with one operational surge."""

        if not machine_id:
            raise InvalidInputError("replacement machine is required")
        if not template_version:
            raise InvalidInputError("planned host replacement requires a host template")
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            initial = _require_internal_pooled_unit(
                units.get_by_capacity_owner_id(capacity_owner_id), unit_ref=capacity_owner_id
            )
            if initial.platform_fleet:
                units.lock_platform_capacity()
            else:
                units.lock_capacity_workspace(workspace_id)
            unit = _require_internal_pooled_unit(
                units.get_by_capacity_owner_id(capacity_owner_id, for_update=True),
                unit_ref=capacity_owner_id,
            )
            if unit.workspace_id != workspace_id:
                raise NotFoundError(f"compute unit not found: {capacity_owner_id}")
            if unit.phase is ComputeUnitPhase.Degraded or unit.provider_state.degraded_reason:
                raise ConflictError("degraded capacity cannot start a replacement")
            if unit.replacement_machine_id:
                if (
                    unit.replacement_machine_id == machine_id
                    and unit.replacement_template_version == template_version
                ):
                    return unit
                raise ConflictError(
                    f"compute pool {unit.name!r} already has a replacement in progress"
                )
            self.require_maintenance_available(session, unit, machine_id="")
            provider, _offer = self.providers.resolved_internal_unit_provider(unit)
            if (
                provider.pooled is None
                or provider.policy is None
                or not provider.policy.can_purchase
            ):
                raise ConflictError("provider policy does not permit replacement capacity")
            provider_machine = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
            if provider_machine is None or provider_machine.pool_id != unit.id:
                raise NotFoundError(f"provider machine not found in compute unit: {machine_id}")
            if not _provider_machine_can_be_replaced(provider_machine):
                raise ConflictError("retired provider machine cannot start a replacement")
            return units.upsert(
                unit.model_copy(
                    update={
                        "replacement_machine_id": machine_id,
                        "replacement_template_version": template_version,
                        "generation": unit.generation + 1,
                        "phase": ComputeUnitPhase.Updating,
                        "status": ComputeUnitPhase.Updating.value,
                    }
                )
            )

    def internal_unit_replaceable_machines(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> set[str]:
        unit = self.providers.get_internal_unit(workspace_id, capacity_owner_id)
        with self.context.database.session() as session:
            records = ComputeProviderInstanceRepository(session).list_for_pool(
                unit.id,
                excluded_statuses=(
                    ReservationStatus.Deleted.value,
                    ReservationStatus.Failed.value,
                    ReservationStatus.Terminating.value,
                ),
            )
        # A machine row exists from the provider's first report; only one whose
        # node enrolled can be asked to hand its work over.
        return {
            record.machine_id
            for record in records
            if record.machine_id
            and record.first_enrolled_at is not None
            and _provider_machine_can_be_replaced(record)
        }

    @contextmanager
    def worker_release_admission(
        self,
        worker: SchedulerWorkerRecord,
        release: ActiveRelease,
        fleet: list[SchedulerWorkerRecord],
    ) -> Iterator[DatabaseSession]:
        with self.worker_maintenance_admission(
            worker.workspace_id, worker.capacity_owner_id, worker.machine_id
        ) as session:
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(worker.capacity_owner_id)
            if unit is None:
                raise NotFoundError("worker capacity owner is missing")
            reserved_target = next(
                (
                    operation
                    for operation in CapacityMaintenanceRepository(session).active_for_pools(
                        [unit.id]
                    )
                    if operation.replacement_machine_id == worker.machine_id
                ),
                None,
            )
            if reserved_target is not None:
                if ContainerRepository(session).count_live_for_machine(worker.machine_id):
                    raise ConflictError("reserved replacement update requires an idle machine")
                if reserved_target.release_generation > release.generation:
                    raise ConflictError("maintenance has observed a newer release")
                self.require_release_headroom(session, unit, worker, release, fleet)
            elif worker.admitted_release_generation > 0:
                replacement_required = self.worker_release_needs_replacement(session, unit, worker)
                operation = self.prepare_release_operation(
                    session,
                    unit,
                    worker,
                    release,
                    fleet,
                    replacement_required=replacement_required,
                )
                if replacement_required:
                    replacement = release_replacement(
                        worker,
                        [
                            item
                            for item in fleet
                            if item.machine_id == operation.replacement_machine_id
                        ],
                        release,
                        now=utc_now(),
                    )
                    if replacement is None:
                        raise ConflictError("worker update is waiting for its reserved replacement")
                if worker.status is not SchedulerWorkerStatus.Draining or operation.phase not in {
                    CapacityMaintenancePhase.Draining,
                    CapacityMaintenancePhase.Verifying,
                }:
                    self.require_release_headroom(session, unit, worker, release, fleet)
                    CapacityMaintenanceRepository(session).transition(
                        operation.id,
                        expected_generation=release.generation,
                        expected_phase=operation.phase,
                        phase=CapacityMaintenancePhase.Draining,
                        now=utc_now(),
                    )
            WorkerReleaseRepository(session).begin_update(
                worker.worker_id, worker.machine_id, release
            )
            yield session

    def prepare_worker_release(
        self,
        worker: SchedulerWorkerRecord,
        release: ActiveRelease,
        fleet: list[SchedulerWorkerRecord],
    ) -> CapacityMaintenanceRecord:
        with self.worker_maintenance_admission(
            worker.workspace_id, worker.capacity_owner_id, worker.machine_id
        ) as session:
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(worker.capacity_owner_id)
            if unit is None:
                raise NotFoundError("worker capacity owner is missing")
            return self.prepare_release_operation(
                session,
                unit,
                worker,
                release,
                fleet,
                replacement_required=self.worker_release_needs_replacement(session, unit, worker),
            )

    @staticmethod
    def worker_release_needs_replacement(
        session: DatabaseSession, unit: ComputeUnitRecord, worker: SchedulerWorkerRecord
    ) -> bool:
        if unit.capacity_mode is not ComputeCapacityMode.Pooled:
            return False
        if not unit.platform_fleet or any(
            free < total
            for free, total in (
                (worker.free_cpu_millicores, worker.total_cpu_millicores),
                (worker.free_memory_mib, worker.total_memory_mib),
                (worker.free_gpu_count, worker.total_gpu_count),
                (worker.free_disk_bytes, worker.total_disk_bytes),
                (worker.free_disk_volumes, worker.total_disk_volumes),
            )
        ):
            return True
        return ContainerRepository(session).count_live_for_machine(worker.machine_id) > 0

    def begin_idle_agent_release(
        self,
        session: DatabaseSession,
        *,
        capacity_owner_id: str,
        machine_id: str,
        worker_id: str,
        release: ActiveRelease,
    ) -> None:
        unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
        if unit is None or ContainerRepository(session).count_live_for_machine(machine_id):
            raise ConflictError("agent update requires an idle machine with a capacity owner")
        active = CapacityMaintenanceRepository(session).active_for_pools([unit.id])
        existing = next((item for item in active if item.source_machine_id == machine_id), None)
        if existing is not None and existing.release_generation > release.generation:
            raise ConflictError("maintenance has observed a newer release")
        reserved_target = next(
            (item for item in active if item.replacement_machine_id == machine_id), None
        )
        if existing is None and reserved_target is None:
            unbound = next(
                (
                    item
                    for item in active
                    if item.kind is CapacityMaintenanceKind.Runtime
                    and item.surge_machines
                    and item.replacement_machine_id is None
                    and item.phase is not CapacityMaintenancePhase.Retiring
                    and item.release_generation <= release.generation
                ),
                None,
            )
            if unbound is not None:
                reserved_target = CapacityMaintenanceRepository(session).transition(
                    unbound.id,
                    expected_generation=unbound.release_generation,
                    expected_phase=unbound.phase,
                    phase=unbound.phase,
                    replacement_machine_id=machine_id,
                    now=utc_now(),
                )
        if reserved_target is not None and reserved_target.release_generation > release.generation:
            raise ConflictError("maintenance has observed a newer release")
        if existing is not None and existing.release_generation < release.generation:
            CapacityMaintenanceRepository(session).retarget(
                existing.id,
                expected_generation=existing.release_generation,
                release_generation=release.generation,
                now=utc_now(),
            )
        if existing is None and reserved_target is None:
            self.start_maintenance(
                session,
                unit,
                machine_id=machine_id,
                release=release,
                kind=CapacityMaintenanceKind.Runtime,
                candidate=MaintenanceCandidate(
                    machine_id, unit.placement.key, hourly_cost_micros=0
                ),
            )
        WorkerReleaseRepository(session).begin_update(worker_id, machine_id, release)

    def prepare_release_operation(
        self,
        session: DatabaseSession,
        unit: ComputeUnitRecord,
        worker: SchedulerWorkerRecord,
        release: ActiveRelease,
        fleet: list[SchedulerWorkerRecord],
        *,
        replacement_required: bool,
    ) -> CapacityMaintenanceRecord:
        repository = CapacityMaintenanceRepository(session)
        active = repository.active_for_placement(unit.placement)
        current = next(
            (item for item in active if item.source_machine_id == worker.machine_id), None
        )
        if current is not None:
            if current.phase is CapacityMaintenancePhase.Retiring:
                raise ConflictError("maintenance is finishing provider cleanup")
            if current.release_generation > release.generation:
                raise ConflictError("maintenance has observed a newer release")
            if current.release_generation < release.generation:
                current = repository.retarget(
                    current.id,
                    expected_generation=current.release_generation,
                    release_generation=release.generation,
                    now=utc_now(),
                )
            if current.replacement_machine_id is not None:
                target = ComputeProviderInstanceRepository(session).get_by_machine(
                    current.replacement_machine_id
                )
                if (target is None or target.status == "deleted") and not any(
                    item.machine_id == current.replacement_machine_id for item in fleet
                ):
                    current = repository.release_deleted_replacement(current, now=utc_now())
                    active = [current if item.id == current.id else item for item in active]
            if current.replacement_machine_id is None and replacement_required:
                reserved = {
                    machine_id
                    for item in active
                    for machine_id in (item.source_machine_id, item.replacement_machine_id)
                    if machine_id is not None
                }
                replacement = release_replacement(
                    worker,
                    [item for item in fleet if item.machine_id not in reserved],
                    release,
                    now=utc_now(),
                )
                if replacement is not None:
                    current = repository.transition(
                        current.id,
                        expected_generation=release.generation,
                        expected_phase=current.phase,
                        phase=current.phase,
                        replacement_machine_id=replacement.machine_id,
                        now=utc_now(),
                    )
                elif not current.surge_machines:
                    cost = self.require_release_surge(session, unit, worker)
                    current = repository.reserve_surge(
                        current,
                        running_cpu_millicores=unit.worker_cpu_millicores
                        if not unit.worker_gpu_count
                        else 0,
                        hourly_cost_micros=cost,
                        now=utc_now(),
                    )
            repository.reserve_allocations(current, worker)
            return current
        reserved = {
            machine_id
            for item in active
            for machine_id in (item.source_machine_id, item.replacement_machine_id)
            if machine_id is not None
        }
        replacement = (
            release_replacement(
                worker,
                [item for item in fleet if item.machine_id not in reserved],
                release,
                now=utc_now(),
            )
            if replacement_required
            else None
        )
        surge = int(replacement_required and replacement is None)
        cost = self.require_release_surge(session, unit, worker) if surge else 0
        operation = self.start_maintenance(
            session,
            unit,
            machine_id=worker.machine_id,
            release=release,
            kind=CapacityMaintenanceKind.Runtime,
            candidate=MaintenanceCandidate(
                machine_id=worker.machine_id,
                market=unit_reserve_market(
                    preemptible=unit.worker_preemptible, gpu_type=unit.worker_gpu_type
                ).key,
                surge_machines=surge,
                running_cpu_millicores=unit.worker_cpu_millicores * surge
                if not unit.worker_gpu_count
                else 0,
                hourly_cost_micros=cost,
                replacement_machine_id=replacement.machine_id if replacement else None,
            ),
        )
        repository.reserve_allocations(operation, worker)
        return operation

    def require_release_surge(
        self, session: DatabaseSession, unit: ComputeUnitRecord, worker: SchedulerWorkerRecord
    ) -> int | None:
        provider, _offer = self.providers.resolved_internal_unit_provider(unit)
        if (
            provider.pooled is None
            or provider.policy is None
            or not provider.policy.can_purchase
            or unit.provider_state.degraded_reason
        ):
            raise ConflictError("provider policy does not permit replacement capacity")
        instance = ComputeProviderInstanceRepository(session).get_by_machine(worker.machine_id)
        if (
            instance is None
            or instance.pool_id != unit.id
            or not _provider_machine_can_be_replaced(instance)
        ):
            raise ConflictError("source machine cannot receive replacement capacity")
        return (
            unit.offer_cost_terms.complete_hourly_cost_micros
            if unit.offer_cost_terms is not None
            else None
        )

    def start_maintenance(
        self,
        session: DatabaseSession,
        unit: ComputeUnitRecord,
        *,
        machine_id: str,
        release: ActiveRelease,
        kind: CapacityMaintenanceKind,
        candidate: MaintenanceCandidate,
    ) -> CapacityMaintenanceRecord:
        units = ComputeUnitRepository(session)
        operations = CapacityMaintenanceRepository(session)
        pool_ids = units.platform_maintenance_pool_ids() if unit.platform_fleet else [unit.id]
        active = operations.active_for_pools(pool_ids)
        if unit.platform_fleet:
            demand = ContainerRepository(session).unplaced_platform_demand()
            market_waiting = (
                (GPU_ANY in demand.gpu_types or unit.worker_gpu_type in demand.gpu_types)
                if unit.worker_gpu_count
                else demand.preemptible_cpu
                if unit.worker_preemptible
                else demand.cpu
            )
            if market_waiting:
                raise ConflictError(
                    "queued work in this market takes precedence over new maintenance"
                )
        ready: dict[str, Capacity] = {}
        required_ready: dict[str, Capacity] = {}
        if kind is CapacityMaintenanceKind.ReserveRefresh:
            rows = units.platform_reserve_rows()
            shapes = {item.id: item for item in rows.units}
            reserved_sources = {item.source_machine_id for item in active}
            for instance in rows.instances:
                if (
                    instance.status != "stopped"
                    or instance.missing
                    or instance.machine_id in reserved_sources
                    or not release.target.accepts(
                        instance.prepared_worker_image, instance.prepared_agent_sha256
                    )
                ):
                    continue
                shape = shapes[instance.unit_id]
                key = unit_reserve_market(
                    preemptible=shape.preemptible, gpu_type=shape.gpu_type
                ).key
                ready[key] = ready.get(key, Capacity()) + machine_capacity(
                    shape.cpu_millicores,
                    shape.memory_mib,
                    shape.gpu_count,
                    reported_memory_mib=shape.reported_memory_mib,
                )
            if self.providers.reserve_state is None:
                raise ConflictError("reserve refresh requires the fleet capacity plan")
            publication = self.providers.reserve_state.published()
            if publication is None:
                raise ConflictError("reserve refresh is waiting for a current fleet capacity plan")
            required_ready = {
                key: target.stopped_target for key, target in publication.markets.items()
            }
        budget = MaintenanceBudget(
            ready=ready,
            required_ready=required_ready,
            reserved_replacements=frozenset(
                machine
                for operation in active
                for machine in (operation.source_machine_id, operation.replacement_machine_id)
                if machine is not None
            ),
        )
        if not plan_maintenance([candidate], budget):
            raise ConflictError(
                "maintenance is waiting for ready capacity or replacement ownership"
            )
        now = utc_now()
        return operations.start(
            CapacityMaintenanceRecord(
                id=str(uuid4()),
                pool_id=unit.id,
                source_machine_id=machine_id,
                release_generation=release.generation,
                kind=kind,
                surge_machines=candidate.surge_machines,
                running_cpu_millicores=candidate.running_cpu_millicores,
                hourly_cost_micros=candidate.hourly_cost_micros,
                replacement_machine_id=candidate.replacement_machine_id,
                created_at=now,
                updated_at=now,
            )
        )

    def require_release_headroom(
        self,
        session: DatabaseSession,
        unit: ComputeUnitRecord,
        worker: SchedulerWorkerRecord,
        release: ActiveRelease,
        fleet: list[SchedulerWorkerRecord],
    ) -> None:
        if not unit.platform_fleet:
            return
        market = unit_reserve_market(
            preemptible=unit.worker_preemptible, gpu_type=unit.worker_gpu_type
        )
        publication = (
            self.providers.reserve_state.published() if self.providers.reserve_state else None
        )
        target = publication.markets.get(market.key) if publication else None
        if target is None:
            raise ConflictError("maintenance is waiting for the platform capacity plan")
        ready = Capacity()
        for member in fleet:
            if (
                member.placement == unit.placement
                and member.preemptible == unit.worker_preemptible
                and member.gpu_type == unit.worker_gpu_type
                and release.target.accepts(member.runtime_image, member.agent_binary_sha256)
                and member.request_intake_status(at=utc_now()) is SchedulerWorkerStatus.Available
            ):
                ready += Capacity(
                    member.free_cpu_millicores, member.free_memory_mib, member.free_gpu_count
                )
        reserved = CapacityMaintenanceRepository(session).reserved_resources(
            placement=unit.placement,
            preemptible=unit.worker_preemptible,
            gpu_type=unit.worker_gpu_type,
        )
        ready = (ready - Capacity(*reserved)).clamped()
        lost = (
            Capacity(worker.free_cpu_millicores, worker.free_memory_mib, worker.free_gpu_count)
            if release.target.accepts(worker.runtime_image, worker.agent_binary_sha256)
            and worker.request_intake_status(at=utc_now()) is SchedulerWorkerStatus.Available
            else Capacity()
        )
        if not plan_maintenance(
            [
                MaintenanceCandidate(
                    worker.machine_id, market.key, unavailable=lost, hourly_cost_micros=0
                )
            ],
            MaintenanceBudget(
                ready={market.key: ready},
                required_ready={market.key: target.warm_target},
            ),
        ):
            raise ConflictError("worker update would consume required warm capacity")

    @contextmanager
    def worker_maintenance_admission(
        self, workspace_id: str, capacity_owner_id: str, machine_id: str
    ) -> Iterator[DatabaseSession]:
        with self.context.database.session() as session:
            repository = ComputeUnitRepository(session)
            unit = repository.get_by_capacity_owner_id(capacity_owner_id)
            if unit is None or unit.workspace_id != workspace_id:
                raise NotFoundError(f"compute unit not found: {capacity_owner_id}")
            if unit.platform_fleet:
                repository.lock_platform_capacity()
            else:
                repository.lock_capacity_workspace(workspace_id)
            unit = repository.get(unit.id, for_update=True)
            if unit is None or unit.workspace_id != workspace_id:
                raise NotFoundError(f"compute unit not found: {capacity_owner_id}")
            if (
                unit.replacement_machine_id
                or unit.id in CapacityRecoveryRepository(session).active_source_units()
            ):
                raise ConflictError("host replacement or interruption recovery takes precedence")
            machine = MachineRepository(session).get(machine_id, workspace_id=workspace_id)
            if machine is None or machine.lifecycle in {
                MachineLifecycle.Stopping,
                MachineLifecycle.Stopped,
                MachineLifecycle.Terminating,
            }:
                raise ConflictError("machine lifecycle does not permit a worker update")
            enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                workspace_id, machine_id, for_update=True
            )
            if (
                enrollment is None
                or enrollment.status is not ComputeMachineEnrollmentStatus.Active
                or enrollment.capacity_owner_id != capacity_owner_id
                or enrollment.capacity_state is not AgentCapacityState.Available
            ):
                raise ConflictError("machine lifecycle does not permit a worker update")
            yield session

    def require_maintenance_available(
        self, session: DatabaseSession, unit: ComputeUnitRecord, *, machine_id: str
    ) -> None:
        candidates = (
            ComputeUnitRepository(session).list_platform_internal(gpu=unit.worker_gpu_count > 0)
            if unit.platform_fleet
            else [unit]
        )
        recovering = CapacityRecoveryRepository(session).active_source_units()
        if any(item.id in recovering for item in candidates):
            raise ConflictError("capacity interruption recovery takes precedence over maintenance")
        if any(item.replacement_machine_id or item.maintenance_active for item in candidates):
            raise ConflictError("capacity maintenance is already in progress")
        if self.worker_update_in_progress(session, candidates, excluding_machine_id=machine_id):
            raise ConflictError("a worker update is already in progress")

    def worker_update_in_progress(
        self,
        session: DatabaseSession,
        candidates: Sequence[ComputeUnitRecord],
        *,
        excluding_machine_id: str = "",
    ) -> bool:
        return WorkerReleaseRepository(session).pools_have_update(
            [candidate.id for candidate in candidates], excluding_machine_id=excluding_machine_id
        )

    def clear_internal_unit_replacement(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
    ) -> ComputeUnitRecord:
        """Clear a settled replacement pair without changing logical capacity."""

        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            unit = _require_internal_pooled_unit(
                units.get_by_capacity_owner_id(capacity_owner_id, for_update=True),
                unit_ref=capacity_owner_id,
            )
            if unit.workspace_id != workspace_id:
                raise NotFoundError(f"compute unit not found: {capacity_owner_id}")
            if not unit.replacement_machine_id:
                return unit
            if unit.replacement_machine_id != machine_id:
                raise ConflictError(f"compute pool {unit.name!r} replacement ownership changed")
            return units.upsert(
                unit.model_copy(
                    update={
                        "replacement_machine_id": "",
                        "replacement_template_version": "",
                        "generation": unit.generation + 1,
                    }
                )
            )

    def internal_unit_draining_machines(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> dict[str, datetime]:
        """Read drain intent from enrollment; unavailable workers can also mean disconnection."""
        with self.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session).list_for_unit(
                workspace_id,
                capacity_owner_id,
            )
        return {
            enrollment.machine_id: enrollment.capacity_observed_at
            for enrollment in enrollments
            if enrollment.machine_id
            and enrollment.capacity_state is AgentCapacityState.Draining
            and enrollment.capacity_observed_at is not None
        }

    def internal_unit_interrupted_machines(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> dict[str, datetime]:
        with self.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session).list_for_unit(
                workspace_id, capacity_owner_id
            )
        return {
            enrollment.machine_id: enrollment.capacity_notice_at
            for enrollment in enrollments
            if enrollment.machine_id
            and enrollment.status is ComputeMachineEnrollmentStatus.Active
            and enrollment.capacity_state
            in {
                AgentCapacityState.Draining,
                AgentCapacityState.Preempting,
                AgentCapacityState.Cordoned,
            }
            and enrollment.capacity_notice_at is not None
        }

    def maintenance_protected_machines(self, capacity_owner_id: str) -> set[str]:
        with self.context.database.session() as session:
            operations = CapacityMaintenanceRepository(session).active_for_pools(
                [capacity_owner_id]
            )
        protected = {
            machine
            for operation in operations
            if operation.phase is not CapacityMaintenancePhase.Retiring
            for machine in (operation.source_machine_id, operation.replacement_machine_id)
            if machine is not None
        }
        if self.providers.reserve_state is not None:
            protected.update(
                machine
                for operation in self.providers.reserve_state.consolidations().values()
                for machine in operation.destination_machine_ids
            )
        return protected

    def recovery_protected_machines(self, capacity_owner_id: str) -> set[str]:
        with self.context.database.session() as session:
            return CapacityRecoveryRepository(session).protected_sources(capacity_owner_id)

    def drain_internal_unit_machine(
        self,
        workspace_id: str,
        machine_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        """Persist drain intent on enrollment so heartbeats cannot restore intake.
        Keep the original timestamp on repeated observations."""
        current_time = _utc(now)
        with self.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.by_machine(workspace_id, machine_id, for_update=True)
            if enrollment is None:
                raise KeyError(f"machine enrollment not found: {machine_id}")
            if enrollment.capacity_state is not AgentCapacityState.Available:
                return False
            enrollments.save(
                enrollment.model_copy(
                    update={
                        "capacity_state": AgentCapacityState.Draining,
                        "capacity_reason": reason,
                        "capacity_observed_at": current_time,
                    }
                )
            )
            WorkerReleaseRepository(session).cancel_machine_update(machine_id)
            machine = MachineRepository(session).get(machine_id, workspace_id=workspace_id)
            if machine is not None and machine_lifecycle_allowed(
                machine.lifecycle, MachineLifecycle.Draining
            ):
                write_machine_lifecycle(
                    session,
                    machine,
                    MachineLifecycle.Draining,
                    workspace_changes=self.providers.workspace_changes,
                    workspace_id=workspace_id,
                    message=reason,
                    now=current_time,
                )
        return True

    def internal_unit_machine_drain_reason(self, workspace_id: str, machine_id: str) -> str | None:
        with self.context.database.session() as session:
            enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                workspace_id, machine_id
            )
            if enrollment is None or enrollment.capacity_state is not AgentCapacityState.Draining:
                return None
            return enrollment.capacity_reason
