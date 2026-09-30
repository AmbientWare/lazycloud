from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from database.repositories.capacity_activations import CapacityActivationRepository
from database.repositories.capacity_maintenance import CapacityMaintenanceRepository
from database.repositories.capacity_recovery import CapacityRecoveryRepository
from database.repositories.capacity_sleep_attempts import CapacitySleepAttemptRepository
from database.repositories.compute import (
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeReserveInstance,
    ComputeUnitRepository,
)
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
)
from database.repositories.worker_releases import WorkerReleaseRepository
from shared.capacity_lifecycle import (
    CapacitySleepMode,
    CapacitySleepObservation,
    CapacitySleepRequest,
)
from shared.capacity_maintenance import (
    CapacityMaintenanceKind,
    CapacityMaintenancePhase,
)
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputeMachineEnrollmentStatus,
    MachineStopPreparationReceipt,
    agent_machine_worker_id,
)
from shared.compute_fleet import MachineLifecycle
from shared.compute_policy import (
    ENDED_UNIT_PHASES,
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitRecord,
    ComputeUnitVisibility,
)
from shared.errors import (
    ConflictError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.releases import ActiveRelease, ReleaseTarget
from shared.timestamps import utc_now

from compute.capacity_errors import (
    CapacityReservationLeaseLostError,
    CapacityReservationLockContendedError,
)
from compute.context import ComputeContext
from compute.fleet_operations import request_machine_stop
from compute.fleet_reserves import (
    machine_capacity,
    unit_reserve_market,
)
from compute.fleet_resources import Capacity
from compute.machine_lifecycle import (
    PREPARED_RESERVE_STATUSES,
    machine_lifecycle_allowed,
    write_machine_lifecycle,
)
from compute.maintenance import CapacityMaintenanceService
from compute.maintenance_policy import MaintenanceCandidate
from compute.offers import ReservationStatus
from compute.pool_provider import PoolProviderService
from compute.provider_machines import (
    _reservation_open,
    provider_unit_operational_capacity,
)
from compute.sleep_lifecycle import observe_sleep, prepare_sleep

LOGGER = logging.getLogger(__name__)
_RESERVE_OWNER = str(uuid5(NAMESPACE_URL, "lazycloud:platform-warm-capacity"))


class ReservePreparationPhase(StrEnum):
    Serving = "serving"
    PreparingCold = "preparing_cold"
    PreparingWarm = "preparing_warm"
    StoppingUsed = "stopping_used"
    ResumePending = "resume_pending"
    ResumeAuthorized = "resume_authorized"


@dataclass(frozen=True, slots=True)
class ReserveAgentPreparation:
    phase: ReservePreparationPhase = ReservePreparationPhase.Serving
    stop_request_id: str = ""
    sleep_request: CapacitySleepRequest | None = None
    sleep_observation_ack: str = ""


def reserve_release_artifacts(release: ReleaseTarget) -> tuple[str, str]:
    """The agent binary and worker image a reserve must hold to resume without updating."""
    return (release.agent.sha256 if release.agent is not None else "", release.worker_image)


@dataclass(frozen=True, slots=True)
class ReserveMachineService:
    context: ComputeContext
    maintenance: CapacityMaintenanceService
    providers: PoolProviderService

    def prepare_reserved_machine(
        self,
        *,
        workspace_id: str,
        machine_id: str,
        credential_id: str,
        credential_generation: int,
        release: ReleaseTarget,
        agent_binary_sha256: str,
        prepared_worker_images: Sequence[str],
        active_worker_images: Mapping[str, str],
        admission_waiting_workers: Collection[str],
        boot_id: str,
        sleep_observation: CapacitySleepObservation | None,
        prepared_stop: MachineStopPreparationReceipt | None,
    ) -> ReserveAgentPreparation:
        with self.context.database.session() as session:
            record = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
        if record is None or record.pool_id is None or record.instance_id is None:
            return ReserveAgentPreparation()
        if (
            record.status not in PREPARED_RESERVE_STATUSES
            and record.status != ReservationStatus.Resuming.value
            and sleep_observation is None
        ):
            return ReserveAgentPreparation()
        with ExitStack() as stack:
            if record.status in PREPARED_RESERVE_STATUSES:
                stack.enter_context(
                    self.providers.required_capacity_owner_mutations().mutation_lock(record.pool_id)
                )
            return self.prepare_machine(
                record=record,
                workspace_id=workspace_id,
                machine_id=machine_id,
                credential_id=credential_id,
                credential_generation=credential_generation,
                release=release,
                agent_binary_sha256=agent_binary_sha256,
                prepared_worker_images=prepared_worker_images,
                active_worker_images=active_worker_images,
                admission_waiting_workers=admission_waiting_workers,
                boot_id=boot_id,
                sleep_observation=sleep_observation,
                prepared_stop=prepared_stop,
            )

    def prepare_machine(
        self,
        *,
        record: ComputeProviderInstanceRecord,
        workspace_id: str,
        machine_id: str,
        credential_id: str,
        credential_generation: int,
        release: ReleaseTarget,
        agent_binary_sha256: str,
        prepared_worker_images: Sequence[str],
        active_worker_images: Mapping[str, str],
        admission_waiting_workers: Collection[str],
        boot_id: str,
        sleep_observation: CapacitySleepObservation | None,
        prepared_stop: MachineStopPreparationReceipt | None,
    ) -> ReserveAgentPreparation:
        """Authenticate preparation evidence and keep retained hosts out of intake."""
        release_agent, release_image = reserve_release_artifacts(release)
        agent_current = not release_agent or agent_binary_sha256 == release_agent
        worker_prepared = release_image in prepared_worker_images
        with self.context.database.session() as session:
            current = ComputeProviderInstanceRepository(session).get_by_machine(
                machine_id, for_update=True
            )
            if (
                current is None
                or current.id != record.id
                or (
                    record.status not in PREPARED_RESERVE_STATUSES
                    and current.status in PREPARED_RESERVE_STATUSES
                )
            ):
                raise UpstreamUnavailableError("reserve state changed during preparation; retry")
            record = current
            if record.pool_id is None or record.instance_id is None:
                return ReserveAgentPreparation()
            preparing = record.status in PREPARED_RESERVE_STATUSES
            resuming = record.status == ReservationStatus.Resuming.value
            if not preparing and not resuming and sleep_observation is None:
                return ReserveAgentPreparation()
            enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                workspace_id, machine_id, for_update=True
            )
            if (
                enrollment is None
                or enrollment.id != credential_id
                or enrollment.credential_generation != credential_generation
                or enrollment.status is not ComputeMachineEnrollmentStatus.Active
            ):
                raise ConflictError("reserve preparation requires the current agent credential")
            unit = ComputeUnitRepository(session).get(record.pool_id)
            machine = MachineRepository(session).get(machine_id, workspace_id=workspace_id)
            if unit is None or machine is None:
                raise ConflictError("reserve preparation lost its capacity owner")
            observed = observe_sleep(
                session, record.id, sleep_observation, boot_id=boot_id, now=utc_now()
            )
            if not preparing and not resuming:
                return ReserveAgentPreparation(sleep_observation_ack=observed.acknowledgment)
            if resuming and record.resume_authorized_at is not None:
                # An earlier stream authorized the resume; the machine serves, and
                # only that stream tells the agent it resumed.
                return ReserveAgentPreparation(sleep_observation_ack=observed.acknowledgment)
            # Returning a used machine requires tenant-storage cleanup. Refreshing
            # a stopped reserve follows preparation even if it served in the past.
            stop_request_id = (
                machine.lifecycle_at.isoformat()
                if machine.lifecycle is MachineLifecycle.Stopping
                and record.first_served_at is not None
                and record.status == ReservationStatus.Stopping.value
                else ""
            )
            stopping_used_machine = bool(stop_request_id)
            # A new boot can precede the provider row's transition to resuming.
            lagging_resume = (
                observed.activated
                and not stopping_used_machine
                and record.status
                in {
                    ReservationStatus.Stopping.value,
                    ReservationStatus.Stopped.value,
                }
            )
            hibernate = preparing and record.hibernates and not stopping_used_machine
            warm = hibernate or lagging_resume
            if stopping_used_machine:
                phase = ReservePreparationPhase.StoppingUsed
            elif lagging_resume:
                phase = ReservePreparationPhase.ResumePending
            elif preparing:
                phase = (
                    ReservePreparationPhase.PreparingWarm
                    if warm
                    else ReservePreparationPhase.PreparingCold
                )
            else:
                phase = ReservePreparationPhase.Serving
            instruction = ReserveAgentPreparation(
                phase, stop_request_id, sleep_observation_ack=observed.acknowledgment
            )
            # A hibernating reserve stops with its worker on the release, built and
            # waiting at its first call; any other stops with none.
            machine_worker = agent_machine_worker_id(machine_id)
            worker_ready = (
                active_worker_images.get(machine_worker) == release_image
                and machine_worker in admission_waiting_workers
                if warm
                else not active_worker_images
            )
            if stopping_used_machine:
                if prepared_stop is None:
                    return instruction
                if prepared_stop.request_id != stop_request_id or active_worker_images:
                    raise ConflictError("stop preparation acknowledgment is stale")
                if ContainerRepository(session).count_live_for_machine(machine_id):
                    raise ConflictError("machine still owns live workloads")
            elif (
                not worker_prepared
                or not agent_current
                or (preparing and not worker_ready)
                or record.status
                in {
                    ReservationStatus.Stopped.value,
                    ReservationStatus.Stopping.value,
                }
                # A GPU reserve proves its image and driver before it stops: the
                # agent's enrollment found the cards through the driver and its
                # container runtime passed preflight on this machine.
                or (
                    preparing
                    and unit.worker_gpu_count
                    and (
                        enrollment.gpu_count < unit.worker_gpu_count
                        or not enrollment.preflight_passed
                    )
                )
            ):
                return instruction
            if preparing:
                if stopping_used_machine and record.status == ReservationStatus.Stopping.value:
                    attempts = CapacitySleepAttemptRepository(session)
                    attempt = attempts.get_current(record.id)
                    if (
                        attempt is not None
                        and attempt.accepted_at is not None
                        and attempt.superseded_at is None
                        and not attempts.was_activated(attempt.id)
                    ):
                        return instruction
                if not boot_id:
                    raise ConflictError("reserve preparation requires the machine boot identity")
                sleep_request, marker_ready = prepare_sleep(
                    session,
                    record.id,
                    boot_id=boot_id,
                    mode=CapacitySleepMode.Hibernate if hibernate else CapacitySleepMode.Stop,
                    now=utc_now(),
                )
                instruction = replace(instruction, sleep_request=sleep_request)
                if not marker_ready:
                    return instruction
            if preparing:
                # A stopped machine can never complete an update it began. Resuming
                # registers a new worker, which must match the active release before
                # it takes requests whether or not an update is recorded.
                WorkerReleaseRepository(session).cancel_machine_update(machine_id)
            target = MachineLifecycle.Stopping if preparing else MachineLifecycle.Joining
            if machine_lifecycle_allowed(machine.lifecycle, target):
                write_machine_lifecycle(
                    session,
                    machine,
                    target,
                    workspace_changes=self.providers.workspace_changes,
                    workspace_id=workspace_id,
                )
            if resuming and enrollment.capacity_notice_at is None:
                ComputeMachineEnrollmentRepository(session).save(
                    enrollment.model_copy(
                        update={
                            "capacity_state": AgentCapacityState.Available,
                            "capacity_reason": "",
                            "capacity_observed_at": utc_now(),
                        }
                    )
                )
            if resuming:
                # Joining opens the fence, so the resumed worker registers without
                # waiting on the provider; the capacity pass finishes the row it marks.
                ComputeProviderInstanceRepository(session).authorize_resume(
                    record.id,
                )
        if resuming:
            return ReserveAgentPreparation(
                ReservePreparationPhase.ResumeAuthorized,
                sleep_observation_ack=observed.acknowledgment,
            )
        try:
            self.finish_reserved_machine_preparation_under_lease(
                workspace_id=workspace_id,
                machine_id=machine_id,
                capacity_owner_id=unit.capacity_owner_id,
                instance_id=record.instance_id,
                prepared_stop=prepared_stop if stopping_used_machine else None,
                sleep_request=instruction.sleep_request,
                prepared_release=(
                    (
                        release_agent if agent_current else "",
                        release_image if worker_prepared else "",
                    )
                    if preparing
                    else None
                ),
            )
        except CapacityReservationLockContendedError as contended:
            # Another pass holds the pool's lease for a few seconds of provider
            # calls; the next stream finishes the bookkeeping.
            LOGGER.info("reserve preparation for %s deferred: %s", machine_id, contended)
        return instruction

    def finish_reserved_machine_preparation_under_lease(
        self,
        *,
        workspace_id: str,
        machine_id: str,
        capacity_owner_id: str,
        instance_id: str,
        prepared_stop: MachineStopPreparationReceipt | None,
        prepared_release: tuple[str, str] | None,
        sleep_request: CapacitySleepRequest | None,
    ) -> None:
        current, provider, offer = self.providers.internal_unit_provider(
            workspace_id, capacity_owner_id
        )
        if provider.pooled is None or current.phase in ENDED_UNIT_PHASES:
            raise ConflictError("reserve preparation owner is no longer active")
        if sleep_request is not None:
            with self.context.database.session() as session:
                record = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
                attempt = (
                    CapacitySleepAttemptRepository(session).get_current(record.id)
                    if record is not None and record.instance_id == instance_id
                    else None
                )
                if (
                    attempt is None
                    or attempt.id != sleep_request.attempt_id
                    or attempt.boot_id != sleep_request.boot_id
                    or attempt.requested_mode is not sleep_request.mode
                    or attempt.marker_observed_at is None
                ):
                    raise UpstreamUnavailableError("sleep preparation was superseded; retry")
        if prepared_stop is not None:
            if sleep_request is None:
                raise ConflictError("stopping a machine requires a prepared sleep attempt")
            if self.providers.scheduler_hooks is None:
                raise UpstreamUnavailableError(
                    "stopping retained capacity requires scheduler worker state"
                )
            with self.providers.required_capacity_owner_mutations().dispatch_lock(
                capacity_owner_id
            ):
                with self.context.database.session() as session:
                    latest = MachineRepository(session).get(machine_id, workspace_id=workspace_id)
                    if (
                        latest is None
                        or latest.lifecycle is not MachineLifecycle.Stopping
                        or latest.lifecycle_at.isoformat() != prepared_stop.request_id
                        or ContainerRepository(session).count_live_for_machine(machine_id)
                    ):
                        raise ConflictError("stop preparation was superseded")
                self.providers.source_cache_lifecycle.acknowledge_machine_cleanup(
                    machine_id=machine_id,
                    worker_id=agent_machine_worker_id(machine_id),
                    generation_id=prepared_stop.cache_generation_id,
                    session_fence=prepared_stop.cache_session_fence,
                    observed_at=utc_now(),
                )
                self.providers.scheduler_hooks.disable_machine(
                    machine_id, "machine returning to stopped reserve"
                )
                snapshot = provider.pooled.stop_machine(
                    self.providers.provider_unit_request(current, offer),
                    instance_id,
                    sleep_request=sleep_request,
                )
                self.providers.machines._apply_pooled_snapshot(
                    current, offer, snapshot, provider=provider.pooled
                )
        else:
            # Applied in the stream that finished the preparation, so the row
            # reads stopping or active without waiting for a capacity pass.
            snapshot = provider.pooled.complete_machine_preparation(
                self.providers.provider_unit_request(current, offer),
                instance_id,
                sleep_request=sleep_request,
            )
            self.providers.machines._apply_pooled_snapshot(
                current, offer, snapshot, provider=provider.pooled
            )
        if prepared_release is not None:
            agent_sha256, worker_image = prepared_release
            with self.context.database.session() as session:
                ComputeProviderInstanceRepository(session).record_prepared_release(
                    machine_id=machine_id,
                    instance_id=instance_id,
                    agent_sha256=agent_sha256,
                    worker_image=worker_image,
                )
                record = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
                if record is not None:
                    CapacityActivationRepository(session).record_prepared(record.id, at=utc_now())

    def release_internal_unit_machine(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
    ) -> ComputeUnitRecord:
        unit, provider, offer = self.providers.internal_unit_provider(
            workspace_id, capacity_owner_id
        )
        if provider.pooled is None:
            raise RuntimeError("internal compute pool does not use pooled capacity")
        market = unit_reserve_market(
            preemptible=unit.worker_preemptible, gpu_type=unit.worker_gpu_type
        )
        publication = (
            self.providers.reserve_state.published() if self.providers.reserve_state else None
        )
        stopped_target = (
            targets.stopped_target
            if publication is not None
            and (targets := publication.markets.get(market.key)) is not None
            else None
        )
        can_retain = (
            unit.platform_fleet
            and stopped_target is not None
            and provider.policy is not None
            and provider.policy.can_purchase
            and any(
                candidate.id == offer.id and candidate.capability_key == offer.capability_key
                for candidate in provider.pooled.list_reserve_offers(
                    root_volume_gib=unit.root_volume_gib
                )
            )
        )
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            if unit.platform_fleet:
                units.lock_platform_capacity()
            current = units.get(unit.id, for_update=True)
            if current is None:
                raise NotFoundError("compute pool disappeared during machine retirement")
            if WorkerReleaseRepository(session).machine_has_update(machine_id):
                enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                    workspace_id, machine_id, for_update=True
                )
                if enrollment is None or enrollment.capacity_state is AgentCapacityState.Available:
                    raise ConflictError("worker update must finish before idle machine retirement")
                WorkerReleaseRepository(session).cancel_machine_update(machine_id)
            instances = ComputeProviderInstanceRepository(session)
            records = instances.list_for_pool(unit.id, for_update=True)
            record = next((item for item in records if item.machine_id == machine_id), None)
            if record is None or record.instance_id is None:
                raise KeyError(f"provider instance for machine not found: {machine_id}")
            if not _reservation_open(record.status):
                return current
            if record.status in {
                ReservationStatus.Preparing.value,
                ReservationStatus.Stopping.value,
                ReservationStatus.Stopped.value,
                ReservationStatus.Resuming.value,
            }:
                return current
            enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                workspace_id, machine_id, for_update=True
            )
            machine = MachineRepository(session).get(machine_id, workspace_id=workspace_id)
            if (
                unit.platform_fleet
                and publication is None
                and not record.terminating_reason
                and current.replacement_machine_id != machine_id
                and (enrollment is None or enrollment.capacity_notice_at is None)
            ):
                raise ConflictError(
                    "machine retirement is waiting for a current fleet capacity plan"
                )
            running = sum(
                item.status
                in {
                    ReservationStatus.Active.value,
                    ReservationStatus.Pending.value,
                    ReservationStatus.Resuming.value,
                }
                for item in records
            )
            desired = (
                current.desired_machines
                if running > current.desired_machines
                else max(current.min_machines, current.desired_machines - 1)
            )
            retained = max(
                current.stopped_machines,
                1
                + sum(
                    item.status
                    in {
                        ReservationStatus.Preparing.value,
                        ReservationStatus.Stopping.value,
                        ReservationStatus.Stopped.value,
                    }
                    for item in records
                ),
            )
            if can_retain and stopped_target is not None:
                # The used machine becomes a reserve only while the market's
                # reserves, without it, fall short of the target the planner set.
                shape = (
                    current.worker_cpu_millicores,
                    current.worker_memory_mib,
                    current.worker_gpu_count,
                )
                held = machine_capacity(
                    *shape, reported_memory_mib=units.reported_node_memory().get(shape, 0)
                ) * (retained - 1)
                for unit_id, count, cpu, memory, reported, gpu in units.platform_stopped_reserves(
                    preemptible=current.worker_preemptible, gpu_type=current.worker_gpu_type
                ):
                    if unit_id != current.id:
                        held = held + (
                            machine_capacity(cpu, memory, gpu, reported_memory_mib=reported) * count
                        )
                can_retain = not held.covers(stopped_target)
            if (
                can_retain
                and record.first_served_at is not None
                and not record.terminating_reason
                and current.replacement_machine_id != machine_id
                and enrollment is not None
                and enrollment.capacity_notice_at is None
                and enrollment.capacity_state is AgentCapacityState.Draining
                and machine is not None
                and machine.lifecycle is MachineLifecycle.Draining
            ):
                if running <= desired:
                    raise ConflictError("machine is still required by the running capacity target")
                return request_machine_stop(
                    session,
                    unit=current,
                    record=record,
                    machine=machine,
                    enrollment=enrollment,
                    desired=desired,
                    stopped=retained,
                    workspace_changes=self.providers.workspace_changes,
                    now=utc_now(),
                )
            if record.terminating_reason != "idle_pool_scale_down":
                replacement = current.replacement_machine_id == machine_id
                live = sum(
                    item.status
                    in {
                        ReservationStatus.Active.value,
                        ReservationStatus.Pending.value,
                        ReservationStatus.Resuming.value,
                    }
                    and item.missing_since is None
                    for item in records
                )
                operational, _maximum = provider_unit_operational_capacity(current)
                target = (
                    current.desired_machines
                    if replacement
                    or live > operational
                    or CapacityRecoveryRepository(session).source_capacity_adjusted(machine_id)
                    else max(current.desired_machines - 1, current.min_machines)
                )
                intent = units.update_capacity(
                    current.id,
                    expected_generation=current.generation,
                    desired_machines=target,
                    max_machines=current.max_machines,
                    observed_machines=current.observed_machines,
                    phase=ComputeUnitPhase.Updating,
                    provider_state=current.provider_state,
                    replacement_machine_id="" if replacement else current.replacement_machine_id,
                    replacement_template_version=(
                        "" if replacement else current.replacement_template_version
                    ),
                )
                if intent is None:
                    raise ConflictError("compute machine retirement intent was superseded")
                current = intent
                self.providers.machines._terminate_provider_record(
                    session,
                    record,
                    clients={},
                    reason="idle_pool_scale_down",
                    message="machine selected for named retirement after draining",
                )
        snapshot = provider.pooled.release_machine(
            self.providers.provider_unit_request(current, offer),
            record.instance_id,
        )
        return self.providers.machines._apply_pooled_snapshot(
            current, offer, snapshot, provider=provider.pooled
        )

    def release_bound_internal_pool_machine(
        self,
        machine_id: str,
        *,
        workspace: str,
    ) -> bool:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            instance = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
            pool = (
                ComputeUnitRepository(session).get(instance.pool_id)
                if instance is not None and instance.pool_id is not None
                else None
            )
        if pool is None:
            return False
        if (
            pool.workspace_id != workspace_id
            or pool.visibility is not ComputeUnitVisibility.Internal
            or pool.capacity_mode is not ComputeCapacityMode.Pooled
        ):
            raise UpstreamUnavailableError("provider machine ownership is inconsistent")
        self.release_internal_unit_machine(workspace_id, pool.capacity_owner_id, machine_id)
        return True

    def refresh_stale_reserves(self, release: ActiveRelease, *, now: datetime) -> list[str]:
        if self.providers.provider_resolver is None:
            return []
        agent_sha256, worker_image = reserve_release_artifacts(release.target)
        with self.context.database.session() as session:
            candidates = ComputeProviderInstanceRepository(session).stale_platform_reserves(
                agent_sha256=agent_sha256,
                worker_image=worker_image,
                release_generation=release.generation,
                now=now,
                limit=100,
            )
        started: list[str] = []
        try:
            with self.providers.required_capacity_owner_mutations().mutation_lock(_RESERVE_OWNER):
                for candidate in candidates:
                    try:
                        if self.refresh_reserve(candidate, release, now=now):
                            started.append(candidate.machine_id)
                    except CapacityReservationLeaseLostError:
                        raise
                    except (CapacityReservationLockContendedError, ConflictError):
                        continue
                    except Exception:
                        LOGGER.exception(
                            "stopped reserve %s could not be refreshed", candidate.machine_id
                        )
        except CapacityReservationLockContendedError:
            pass
        return started

    def refresh_reserve(
        self,
        candidate: ComputeReserveInstance,
        release: ActiveRelease,
        *,
        now: datetime,
    ) -> bool:
        with self.providers.required_capacity_owner_mutations().mutation_lock(candidate.pool_id):
            with self.context.database.session() as session:
                units = ComputeUnitRepository(session)
                units.lock_platform_capacity()
                unit = units.get(candidate.pool_id, for_update=True)
                if unit is None or unit.phase in ENDED_UNIT_PHASES:
                    return False
                repository = CapacityMaintenanceRepository(session)
                existing = next(
                    (
                        item
                        for item in repository.active_for_pools([unit.id])
                        if item.source_machine_id == candidate.machine_id
                    ),
                    None,
                )
                if existing is not None and (
                    existing.kind is not CapacityMaintenanceKind.ReserveRefresh
                    or existing.release_generation > release.generation
                    or (
                        existing.phase is not CapacityMaintenancePhase.Planned
                        and now
                        < existing.updated_at + timedelta(seconds=unit.registration_timeout_seconds)
                    )
                ):
                    return False
                record = ComputeProviderInstanceRepository(session).get_by_machine(
                    candidate.machine_id
                )
                if record is None or record.status != "stopped":
                    return False
                provider, offer = self.providers.resolved_internal_unit_provider(unit)
                if (
                    provider.pooled is None
                    or provider.policy is None
                    or not provider.policy.can_purchase
                ):
                    return False
                shape = (unit.worker_cpu_millicores, unit.worker_memory_mib, unit.worker_gpu_count)
                lost = (
                    machine_capacity(
                        *shape, reported_memory_mib=units.reported_node_memory().get(shape, 0)
                    )
                    if release.target.accepts(
                        record.prepared_worker_image, record.prepared_agent_sha256
                    )
                    else Capacity()
                )
                if existing is not None and existing.release_generation < release.generation:
                    existing = repository.retarget(
                        existing.id,
                        expected_generation=existing.release_generation,
                        release_generation=release.generation,
                        now=now,
                    )
                operation = existing or self.maintenance.start_maintenance(
                    session,
                    unit,
                    machine_id=candidate.machine_id,
                    release=release,
                    kind=CapacityMaintenanceKind.ReserveRefresh,
                    candidate=MaintenanceCandidate(
                        candidate.machine_id,
                        unit_reserve_market(
                            preemptible=unit.worker_preemptible, gpu_type=unit.worker_gpu_type
                        ).key,
                        unavailable=lost,
                        running_cpu_millicores=unit.worker_cpu_millicores
                        if not unit.worker_gpu_count
                        else 0,
                        hourly_cost_micros=offer.cost_terms.complete_hourly_cost_micros
                        if offer.cost_terms
                        else None,
                    ),
                )
            try:
                snapshot = provider.pooled.refresh_machine(
                    self.providers.provider_unit_request(unit, offer),
                    candidate.instance_id,
                )
                self.providers.machines._apply_pooled_snapshot(
                    unit,
                    offer,
                    snapshot,
                    provider=provider.pooled,
                    now=now,
                )
                with self.context.database.session() as session:
                    CapacityMaintenanceRepository(session).transition(
                        operation.id,
                        expected_generation=release.generation,
                        expected_phase=operation.phase,
                        phase=CapacityMaintenancePhase.Preparing,
                        now=now,
                    )
                    machine = MachineRepository(session).get(
                        candidate.machine_id, workspace_id=unit.workspace_id
                    )
                    if machine is not None and machine_lifecycle_allowed(
                        machine.lifecycle, MachineLifecycle.Resuming
                    ):
                        write_machine_lifecycle(
                            session,
                            machine,
                            MachineLifecycle.Resuming,
                            workspace_changes=self.providers.workspace_changes,
                            workspace_id=unit.workspace_id,
                            message="Preparing the stopped reserve for the current release",
                            now=now,
                        )
            except Exception as exc:
                with self.context.database.session() as session:
                    CapacityMaintenanceRepository(session).transition(
                        operation.id,
                        expected_generation=release.generation,
                        expected_phase=operation.phase,
                        phase=CapacityMaintenancePhase.Failed,
                        reason=f"reserve preparation failed: {type(exc).__name__}",
                        now=now,
                    )
                raise
        return True
