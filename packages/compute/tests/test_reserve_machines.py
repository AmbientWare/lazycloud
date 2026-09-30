from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from compute.capacity_errors import CapacityReservationLockContendedError
from compute.fleet_policy import (
    FleetCapacityPolicy,
    HeadroomTarget,
    MarketReserve,
    MarketReservePlan,
)
from compute.fleet_resources import Capacity, ReserveMarket
from compute.machine_lifecycle import reserve_awaits_resume, write_machine_lifecycle
from compute.offers import ReservationStatus
from compute.providers import (
    ProviderCapacityPhase,
    ProviderOfferEligibility,
    ProviderUnitInstance,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
)
from compute.release_status import ComputeReleaseStatusService
from compute.reserve_machines import ReserveAgentPreparation, ReservePreparationPhase
from compute.reserve_state import RedisFleetReserveState
from compute.service import ComputeServices
from database.context import ServiceContext
from database.repositories.capacity_activations import CapacityActivationRepository
from database.repositories.capacity_maintenance import CapacityMaintenanceRepository
from database.repositories.capacity_sleep_attempts import CapacitySleepAttemptRepository
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRecord,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import MachineRepository
from database.repositories.worker_releases import WorkerReleaseRepository
from packages.compute.tests.pooled_fixtures import (
    _bootstrap,
    _MutationLeases,
    _PooledProvider,
    _Resolver,
    _SchedulerHooks,
    _seed_serving_machine,
)
from provider_aws.managed_pool import Boto3AwsManagedPoolClientProvider
from provider_clients.workspace_compute import WorkspaceComputeProviderResolver
from shared.capacity_lifecycle import (
    CapacityActivationKind,
    CapacitySleepMode,
    CapacitySleepObservation,
    CapacitySleepRequest,
)
from shared.capacity_maintenance import CapacityMaintenancePhase
from shared.compute_enrollment import (
    AgentCapacityState,
    MachineStopPreparationReceipt,
    agent_machine_worker_id,
)
from shared.compute_fleet import MachineLifecycle
from shared.compute_policy import ComputeUnitPhase, ComputeUnitRecord, UnitName
from shared.errors import ConflictError
from shared.releases import ActiveRelease, AgentArtifact, ReleaseMachinePhase, ReleaseTarget
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from tests.real_redis import RealRedisActors

_RESERVE_INSTANCE = "i-reserve0000000000"


@dataclass(slots=True)
class _ReserveProvider(_PooledProvider):
    reserve_status: str = "stopped"
    refreshed: list[str] = field(default_factory=list)
    prepared: list[str] = field(default_factory=list)
    reclaimed: bool = False

    def refresh_machine(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        self.refreshed.append(provider_instance_id)
        self.reserve_status = "preparing"
        return self._snapshot(request)

    def complete_machine_preparation(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        *,
        sleep_request: CapacitySleepRequest | None,
    ) -> ProviderUnitSnapshot:
        if self.reclaimed:
            raise ValueError("instance is not owned by this retained pool")
        self.prepared.append(provider_instance_id)
        self.reserve_status = "active" if self.reserve_status == "resuming" else "stopping"
        return self._snapshot(request)

    def _snapshot(
        self,
        request: ProviderUnitRequest,
        *,
        phase: ProviderCapacityPhase = ProviderCapacityPhase.Ready,
    ) -> ProviderUnitSnapshot:
        snapshot = _PooledProvider._snapshot(self, request, phase=phase)
        reserve = ProviderUnitInstance(
            provider_instance_id=_RESERVE_INSTANCE,
            status=self.reserve_status,
            storage_volume_ids=("vol-reserve",),
        )
        return snapshot.model_copy(update={"instances": [*snapshot.instances, reserve]})


@dataclass(slots=True)
class _ContendedLeases(_MutationLeases):
    contended: set[str] = field(default_factory=set)

    @contextmanager
    def mutation_lock(self, capacity_owner_id: str) -> Iterator[None]:
        if capacity_owner_id in self.contended:
            raise CapacityReservationLockContendedError(f"{capacity_owner_id} is held")
        with _MutationLeases.mutation_lock(self, capacity_owner_id):
            yield


def _stopped_reserve(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    *,
    now: datetime,
) -> tuple[ComputeServices, _ReserveProvider, _ContendedLeases, ComputeUnitRecord, str]:
    provider = _ReserveProvider()
    provider.reserve_offers = (provider.offer,)
    resolved = _Resolver(
        provider,
        service_context,
        allowed_offers=(
            ProviderOfferEligibility(
                region=provider.offer.region,
                instance_type=provider.offer.instance_type,
                preemptible=False,
            ),
        ),
    )._resolved()
    leases = _ContendedLeases()
    compute = ComputeServices.create(
        service_context,
        provider_resolver=WorkspaceComputeProviderResolver(
            connections=lambda _workspace: (),
            capacity_workspace=lambda _connection: "",
            binaries_by_region={},
            client_provider=Boto3AwsManagedPoolClientProvider.from_default_chain(),
            platform_providers=lambda: (resolved,),
        ),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=leases,
        scheduler_hooks=_SchedulerHooks(),
        reserve_state=RedisFleetReserveState(real_redis_actors.client()),
        fleet_policy=FleetCapacityPolicy(
            spot=MarketReserve(),
            on_demand=MarketReserve(stopped=HeadroomTarget(floor=Capacity(1_000, 1_024))),
            gpu={},
        ),
    )
    compute.reserves.reconcile_platform_reserves(now=now)
    with service_context.database.session() as session:
        [unit] = ComputeUnitRepository(session).list_platform_internal()
    compute.reconciliation.reconcile_unit_capacity(unit.id, now=now)
    with service_context.database.session() as session:
        stopped = ComputeProviderInstanceRepository(session).get_by_machine(
            next(
                record.machine_id
                for record in ComputeProviderInstanceRepository(session).list_for_pool(unit.id)
                if record.instance_id == _RESERVE_INSTANCE and record.machine_id is not None
            )
        )
    assert stopped is not None and stopped.machine_id is not None
    assert stopped.status == ReservationStatus.Stopped.value
    with service_context.database.session() as session:
        machines = MachineRepository(session)
        machine = machines.get(stopped.machine_id, workspace_id=unit.workspace_id)
        assert machine is not None
        machines.upsert(
            machine.model_copy(update={"lifecycle": MachineLifecycle.Stopped}),
            workspace_id=unit.workspace_id,
        )
    return compute, provider, leases, unit, stopped.machine_id


def _reserve_enrollment(
    service_context: ServiceContext, unit: ComputeUnitRecord, machine_id: str, *, now: datetime
) -> ComputeMachineEnrollmentRecord:
    with service_context.database.session() as session:
        return ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=None,
                workspace_id=unit.workspace_id,
                capacity_owner_id=unit.capacity_owner_id,
                placement=unit.placement,
                machine_id=machine_id,
                machine_fingerprint_hash="d" * 64,
                credential_hash="e" * 64,
                last_join_at=now,
            )
        )


_RESERVE_RELEASE = ReleaseTarget(
    version="2",
    source_revision="new",
    worker_image="registry.example/worker:new",
    agent=AgentArtifact(url="https://example.test/agent", sha256="a" * 64, size_bytes=1),
)


@pytest.mark.parametrize("resumed", [False, True])
def test_operator_stop_preserves_machine_and_storage_and_cannot_resume_pending_stop(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    resumed: bool,
) -> None:
    now = datetime.now(UTC)
    compute, provider, _leases, unit, machine_id = _stopped_reserve(
        service_context, real_redis_actors, now=now
    )
    _reserve_enrollment(service_context, unit, machine_id, now=now)
    provider.reserve_status = "active"
    with service_context.database.session() as session:
        unit = ComputeUnitRepository(session).upsert(
            unit.model_copy(
                update={
                    "desired_machines": 1,
                    "min_machines": 1,
                    "initial_machines": 1,
                    "stopped_machines": 0,
                }
            )
        )
        instances = ComputeProviderInstanceRepository(session)
        before = instances.get_by_machine(machine_id)
        assert before is not None
        attempts = CapacitySleepAttemptRepository(session)
        prior_sleep = attempts.begin(
            before.id,
            attempt_id=str(uuid4()),
            boot_id=str(uuid4()) if resumed else "",
            requested_mode=CapacitySleepMode.Hibernate,
            requested_at=now - timedelta(minutes=2) if resumed else None,
            observed_at=now - timedelta(minutes=2),
        )
        attempts.accept(
            prior_sleep.id, mode=CapacitySleepMode.Hibernate, at=now - timedelta(minutes=2)
        )
        if resumed:
            CapacityActivationRepository(session).observe(
                before.id,
                requested_at=now - timedelta(minutes=1),
                kind=CapacityActivationKind.Resume,
                provider_running_at=now,
                observed_at=now,
                sleep_attempt_id=prior_sleep.id,
            )
        instances.upsert(before.model_copy(update={"status": "active", "first_served_at": now}))
        machines = MachineRepository(session)
        machine = machines.get(machine_id, workspace_id=unit.workspace_id)
        assert machine is not None
        machines.upsert(
            machine.model_copy(update={"lifecycle": MachineLifecycle.Ready}),
            workspace_id=unit.workspace_id,
        )

    operations = compute.operations
    stopped = operations.stop_machine(unit_id=unit.id, machine_id=machine_id)
    repeated = operations.stop_machine(unit_id=unit.id, machine_id=machine_id)
    assert repeated.generation == stopped.generation
    assert repeated.desired_machines == repeated.min_machines == 0
    assert repeated.stopped_machines == 1
    if not resumed:
        compute.providers.machines._apply_pooled_snapshot(
            stopped,
            provider.offer,
            ProviderUnitSnapshot(
                phase=ProviderCapacityPhase.Ready,
                desired_machines=0,
                stopped_machines=1,
                max_machines=stopped.max_machines,
                observed_machines=0,
                instances=[
                    ProviderUnitInstance(
                        provider_instance_id=_RESERVE_INSTANCE,
                        status="preparing",
                        storage_volume_ids=before.storage_volume_ids,
                        sleep_recovery_observed_at=now,
                    )
                ],
            ),
            provider=provider,
        )
    with service_context.database.session() as session:
        after = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
        machine = MachineRepository(session).get(machine_id, workspace_id=unit.workspace_id)
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            unit.workspace_id, machine_id
        )
    assert after is not None and after.id == before.id and after.instance_id == before.instance_id
    assert after.storage_volume_ids == before.storage_volume_ids
    assert after.status == "stopping" and after.provider_storage_destroyed_at is None
    assert machine is not None and machine.lifecycle is MachineLifecycle.Stopping
    assert enrollment is not None and enrollment.capacity_state is AgentCapacityState.Draining
    preparation = compute.reserve_machines.prepare_reserved_machine(
        workspace_id=unit.workspace_id,
        machine_id=machine_id,
        credential_id=enrollment.id,
        credential_generation=enrollment.credential_generation,
        release=_RESERVE_RELEASE,
        agent_binary_sha256="a" * 64,
        prepared_worker_images=[_RESERVE_RELEASE.worker_image],
        active_worker_images={},
        admission_waiting_workers=[],
        boot_id=prior_sleep.boot_id or str(uuid4()),
        sleep_observation=None,
        prepared_stop=MachineStopPreparationReceipt(request_id=machine.lifecycle_at.isoformat()),
    )
    assert preparation.phase is ReservePreparationPhase.StoppingUsed
    assert preparation.sleep_request is not None
    assert preparation.sleep_request.attempt_id != prior_sleep.id
    assert preparation.sleep_request.mode is CapacitySleepMode.Stop
    with pytest.raises(ConflictError, match="exceeds existing stopped capacity"):
        operations.resume_unit(unit_id=unit.id, desired=1)
    unchanged = compute.providers.get_internal_unit(unit.workspace_id, unit.id)
    assert unchanged.generation == stopped.generation
    assert unchanged.desired_machines == 0 and unchanged.stopped_machines == 1


def test_reserve_retirement_preserves_ready_floor_across_pools_and_pending_cleanup(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime.now(UTC)
    compute, _provider, _leases, first, machine_id = _stopped_reserve(
        service_context, real_redis_actors, now=now
    )
    release = ActiveRelease(
        generation=2, manifest_url="https://example.test/release", target=_RESERVE_RELEASE
    )
    second_id = str(uuid4())
    assert release.target.agent is not None
    second = first.model_copy(
        update={
            "id": second_id,
            "capacity_owner_id": second_id,
            "name": UnitName("second-reserve"),
            "capability_key": "second-reserve",
        }
    )
    with service_context.database.session() as session:
        ComputeUnitRepository(session).upsert(second)
        instances = ComputeProviderInstanceRepository(session)
        original = instances.get_by_machine(machine_id)
        assert original is not None
        prepared = original.model_copy(
            update={
                "prepared_worker_image": release.target.worker_image,
                "prepared_agent_sha256": release.target.agent.sha256,
            }
        )
        instances.upsert(prepared)
        instances.upsert(
            prepared.model_copy(
                update={
                    "id": str(uuid4()),
                    "pool_id": second.id,
                    "instance_id": "i-secondreserve000",
                    "machine_id": None,
                }
            )
        )
    plan = MarketReservePlan(
        market=ReserveMarket(False),
        load=Capacity(),
        quiet=True,
        warm_target=Capacity(),
        warm_free=Capacity(),
        stopped_target=Capacity(1_000, 1_024),
        stopped_capacity=Capacity(2_000, 2_048),
    )
    compute.reserves.set_stopped_reserves(
        {first.id: 0, second.id: 0}, plan=plan, release=release, now=now
    )
    with service_context.database.session() as session:
        units = ComputeUnitRepository(session)
        retired = units.get(first.id)
        retained = units.get(second.id)
        assert retired is not None and retired.stopped_machines == 0
        assert retained is not None and retained.stopped_machines == 1
        instances = ComputeProviderInstanceRepository(session)
        instances.upsert(prepared)
        units.upsert(
            retired.model_copy(
                update={"retiring_stopped_machines": 1, "phase": ComputeUnitPhase.Ready}
            )
        )
    compute.reserves.set_stopped_reserves({second.id: 0}, plan=plan, release=release, now=now)
    with service_context.database.session() as session:
        retained = ComputeUnitRepository(session).get(second.id)
    assert retained is not None and retained.stopped_machines == 1


@pytest.mark.parametrize("controller_exit", [False, True])
def test_stopped_reserve_from_an_older_release_is_prepared_again_and_records_the_release(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    monkeypatch: pytest.MonkeyPatch,
    controller_exit: bool,
) -> None:
    now = datetime.now(UTC)
    compute, provider, leases, unit, machine_id = _stopped_reserve(
        service_context, real_redis_actors, now=now
    )
    release = ActiveRelease(
        generation=2, manifest_url="https://example.test/release", target=_RESERVE_RELEASE
    )

    def refuse_refresh(
        self: _ReserveProvider, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        if controller_exit:
            raise SystemExit("controller exited after committing refresh intent")
        raise RuntimeError("provider unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(_ReserveProvider, "refresh_machine", refuse_refresh)
        if controller_exit:
            with pytest.raises(SystemExit):
                compute.reserve_machines.refresh_stale_reserves(release, now=now)
        else:
            assert compute.reserve_machines.refresh_stale_reserves(release, now=now) == []
    with service_context.database.session() as session:
        repository = CapacityMaintenanceRepository(session)
        [failed] = repository.active_for_pools([unit.id])
        assert failed.phase is (
            CapacityMaintenancePhase.Planned if controller_exit else CapacityMaintenancePhase.Failed
        )
        committed = repository.commitments([unit.id])
    if not controller_exit:
        assert compute.reserve_machines.refresh_stale_reserves(release, now=now) == []
        now += timedelta(seconds=unit.registration_timeout_seconds)
    with service_context.database.session() as session:
        units = ComputeUnitRepository(session)
        current = units.get(unit.id)
        assert current is not None
        units.upsert(
            current.model_copy(
                update={
                    "phase": ComputeUnitPhase.Degraded,
                    "provider_state": current.provider_state.model_copy(
                        update={
                            "degraded_reason": "provider_acquisition_rejected",
                            "degraded_at": now,
                        }
                    ),
                }
            )
        )
    assert compute.reserve_machines.refresh_stale_reserves(release, now=now) == [machine_id]
    with service_context.database.session() as session:
        repository = CapacityMaintenanceRepository(session)
        [retry] = repository.active_for_pools([unit.id])
        assert retry.id == failed.id
        assert repository.commitments([unit.id]) == committed
    assert provider.refreshed == [_RESERVE_INSTANCE]
    assert compute.reserve_machines.refresh_stale_reserves(release, now=now) == []

    with service_context.database.session() as session:
        instances = ComputeProviderInstanceRepository(session)
        refreshing = instances.get_by_machine(machine_id)
        assert refreshing is not None
        instances.upsert(refreshing.model_copy(update={"first_served_at": now}))
    enrollment = _reserve_enrollment(service_context, unit, machine_id, now=now)

    observation: CapacitySleepObservation | None = None
    boot_id = str(uuid4())

    def prepare() -> ReserveAgentPreparation:
        return compute.reserve_machines.prepare_reserved_machine(
            workspace_id=unit.workspace_id,
            machine_id=machine_id,
            credential_id=enrollment.id,
            credential_generation=enrollment.credential_generation,
            release=_RESERVE_RELEASE,
            agent_binary_sha256="a" * 64,
            prepared_worker_images=[_RESERVE_RELEASE.worker_image],
            active_worker_images={},
            admission_waiting_workers=[],
            boot_id=boot_id,
            sleep_observation=observation,
            prepared_stop=None,
        )

    leases.contended.add(unit.capacity_owner_id)
    with pytest.raises(CapacityReservationLockContendedError):
        prepare()
    assert provider.prepared == []
    leases.contended.clear()
    preparing = prepare()
    assert preparing.phase is ReservePreparationPhase.PreparingCold
    assert preparing.sleep_request is not None
    assert provider.prepared == []
    observation = CapacitySleepObservation(
        attempt_id=preparing.sleep_request.attempt_id, boot_id=boot_id
    )
    assert prepare().phase is ReservePreparationPhase.PreparingCold
    assert provider.prepared == [_RESERVE_INSTANCE]
    with service_context.database.session() as session:
        prepared = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
    assert prepared is not None
    assert (prepared.prepared_agent_sha256, prepared.prepared_worker_image) == (
        "a" * 64,
        "registry.example/worker:new",
    )
    release = ActiveRelease(
        generation=2, manifest_url="https://example.test/release", target=_RESERVE_RELEASE
    )
    assert not ComputeReleaseStatusService(service_context.database).read(release, []).complete
    provider.reserve_status = "stopped"
    compute.reconciliation.reconcile_unit_capacity(unit.id, now=now)
    compute.rollouts.reconcile(release, [], now=now)
    status = ComputeReleaseStatusService(service_context.database).read(release, [])
    assert status.complete
    assert status.machines[0].phase is ReleaseMachinePhase.Current


def test_a_resumed_reserve_registers_no_worker_until_its_stream_authorizes_the_resume(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime.now(UTC)
    compute, provider, _leases, unit, machine_id = _stopped_reserve(
        service_context, real_redis_actors, now=now
    )
    enrollment = _reserve_enrollment(service_context, unit, machine_id, now=now)

    def awaits_resume() -> bool:
        with service_context.database.session() as session:
            return reserve_awaits_resume(
                session, machine_id=machine_id, workspace_id=unit.workspace_id
            )

    assert awaits_resume()
    provider.reserve_status = "resuming"
    compute.reconciliation.reconcile_unit_capacity(unit.id, now=now)
    assert awaits_resume()
    compute.machines.record_provider_node_lifecycle(
        pool_id=unit.id,
        provider_instance_id=_RESERVE_INSTANCE,
        lifecycle=MachineLifecycle.Joining,
        failure_reason=None,
    )
    assert awaits_resume()

    # A resuming row no stream authorized, such as a new machine still booting,
    # is left for its own stream; the pass finishes only authorized resumes.
    with service_context.database.session() as session:
        machines = MachineRepository(session)
        resuming = machines.get(machine_id, workspace_id=unit.workspace_id)
        assert resuming is not None
        machines.upsert(
            resuming.model_copy(update={"lifecycle": MachineLifecycle.Booting}),
            workspace_id=unit.workspace_id,
        )
    compute.reconciliation.reconcile_unit_capacity(unit.id, now=now)
    assert provider.prepared == []
    with service_context.database.session() as session:
        MachineRepository(session).upsert(resuming, workspace_id=unit.workspace_id)

    def stream(agent_binary_sha256: str = "a" * 64) -> ReserveAgentPreparation:
        return compute.reserve_machines.prepare_reserved_machine(
            workspace_id=unit.workspace_id,
            machine_id=machine_id,
            credential_id=enrollment.id,
            credential_generation=enrollment.credential_generation,
            release=_RESERVE_RELEASE,
            agent_binary_sha256=agent_binary_sha256,
            prepared_worker_images=[_RESERVE_RELEASE.worker_image],
            active_worker_images={
                agent_machine_worker_id(machine_id): _RESERVE_RELEASE.worker_image
            },
            admission_waiting_workers=[agent_machine_worker_id(machine_id)],
            boot_id=str(uuid4()),
            sleep_observation=None,
            prepared_stop=None,
        )

    def observed() -> tuple[str, MachineLifecycle]:
        with service_context.database.session() as session:
            record = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
            machine = MachineRepository(session).get(machine_id, workspace_id=unit.workspace_id)
        assert record is not None and machine is not None
        return record.status, machine.lifecycle

    # The authorizing stream opens the fence without waiting on the provider.
    assert stream().phase is ReservePreparationPhase.ResumeAuthorized
    assert not awaits_resume()
    assert observed() == (ReservationStatus.Resuming.value, MachineLifecycle.Joining)

    # Only the authorizing stream tells the agent it resumed. A later one, even
    # from an agent updated past the release, leaves the row to the capacity
    # pass, which finishes it and leaves a machine its heartbeat made ready.
    with service_context.database.session() as session:
        machine = MachineRepository(session).get(machine_id, workspace_id=unit.workspace_id)
        assert machine is not None
        write_machine_lifecycle(
            session,
            machine,
            MachineLifecycle.Ready,
            workspace_changes=compute.providers.workspace_changes,
        )
    assert stream(agent_binary_sha256="b" * 64).phase is ReservePreparationPhase.Serving
    assert observed() == (ReservationStatus.Resuming.value, MachineLifecycle.Ready)
    # A resume the provider cannot finish, such as a reclaimed instance, leaves
    # the row for a later pass and does not fail this one.
    provider.reclaimed = True
    reconciled = compute.reconciliation.reconcile_unit_capacity(unit.id, now=now)
    assert reconciled is not None and reconciled.provider_state.degraded_reason is None
    assert observed() == (ReservationStatus.Resuming.value, MachineLifecycle.Ready)
    provider.reclaimed = False
    compute.reconciliation.reconcile_unit_capacity(unit.id, now=now)
    assert observed() == (ReservationStatus.Active.value, MachineLifecycle.Ready)
    with service_context.database.session() as session:
        machine = MachineRepository(session).get(machine_id, workspace_id=unit.workspace_id)
    assert machine is not None

    # A serving machine left in a reserve phase by a stop no one recorded still
    # joins on its own report, since no stream will authorize it again.
    with service_context.database.session() as session:
        write_machine_lifecycle(
            session,
            machine,
            MachineLifecycle.Stopping,
            workspace_changes=compute.providers.workspace_changes,
        )
    joined = compute.machines.record_provider_node_lifecycle(
        pool_id=unit.id,
        provider_instance_id=_RESERVE_INSTANCE,
        lifecycle=MachineLifecycle.Joining,
        failure_reason=None,
    )
    assert joined.lifecycle is MachineLifecycle.Joining


def test_idle_platform_worker_updates_without_buying_capacity_in_a_degraded_pool(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    now = datetime.now(UTC)
    compute, _provider, _leases, unit, machine_id = _stopped_reserve(
        service_context, real_redis_actors, now=now
    )
    _seed_serving_machine(
        service_context,
        unit,
        _SchedulerHooks(),
        machine_id=machine_id,
        instance_id=_RESERVE_INSTANCE,
        now=now,
    )
    with service_context.database.session() as session:
        units = ComputeUnitRepository(session)
        unit = units.upsert(
            unit.model_copy(
                update={
                    "phase": ComputeUnitPhase.Degraded,
                    "provider_state": unit.provider_state.model_copy(
                        update={
                            "degraded_reason": "provider_acquisition_rejected",
                            "degraded_at": now,
                        }
                    ),
                }
            )
        )
    source = SchedulerWorkerRecord(
        worker_id=agent_machine_worker_id(machine_id),
        machine_id=machine_id,
        workspace_id=unit.workspace_id,
        capacity_owner_id=unit.id,
        placement=unit.placement,
        runtime_image="worker:old",
        agent_binary_sha256="b" * 64,
        admitted_release_generation=1,
        status=SchedulerWorkerStatus.Available,
        request_poll_expires_at=now + timedelta(minutes=5),
        total_cpu_millicores=1000,
        free_cpu_millicores=1000,
    )
    release = ActiveRelease(
        generation=2,
        manifest_url="https://example.test/release",
        target=_RESERVE_RELEASE,
    )
    operation = compute.maintenance.prepare_worker_release(source, release, [source])
    assert operation.surge_machines == 0
    assert operation.replacement_machine_id is None
    busy = source.model_copy(update={"free_cpu_millicores": 0})
    with (
        pytest.raises(ConflictError, match="replacement capacity"),
        compute.maintenance.worker_release_admission(busy, release, [busy]),
    ):
        pass
    with compute.maintenance.worker_release_admission(source, release, [source]):
        pass
    with service_context.database.session() as session:
        assert WorkerReleaseRepository(session).machine_has_update(machine_id)
        [admitted] = CapacityMaintenanceRepository(session).active_for_pools([unit.id])
        current = ComputeUnitRepository(session).get(unit.id)
    assert admitted.phase is CapacityMaintenancePhase.Draining
    assert admitted.surge_machines == 0
    assert current is not None
    assert current.provider_state.degraded_reason == "provider_acquisition_rejected"
