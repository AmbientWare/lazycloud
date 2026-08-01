from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from compute.agent_control import TailnetConfig, agent_machine_worker_id, hash_compute_token
from compute.billing import BillingCreditRequest, BillingDecision, ManagedUsage
from compute.offers import ComputeOffer
from compute.projection import PoolConfig, PrivatePoolState
from compute.providers import (
    DirectMachineLaunchRequest,
    ProviderMachineReference,
    ProviderMachineStatus,
    ProviderReconcileResult,
)
from compute.reclaim import ComputeReclaimPolicy
from database.repositories.compute import (
    ComputeCapacityOperationRepository,
    ComputeCapacityRequestRepository,
    ComputeJoinCredentialRecord,
    ComputeJoinCredentialRepository,
    ComputeLedgerRecord,
    ComputeLedgerRepository,
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
    ComputePoolRepository,
    ComputeProviderInstanceRepository,
)
from database.repositories.orchestration import MachineRepository, WorkerRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from gateway.http import JoinAgentRequest
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.state import RedisSchedulerWorkerRepository
from shared.capacity import (
    CapacityAcquisitionPlanningRequest,
    CapacityAcquisitionRequest,
    CapacityAcquisitionShape,
    CapacityAcquisitionStatus,
    CapacityFailureCode,
    CapacityReleaseRequest,
)
from shared.compute_enrollment import (
    ComputeCredentialStatus,
    ComputeMachineEnrollmentStatus,
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
    MachineReadinessPhase,
)
from shared.compute_fleet import Machine, ResourceStatus, Worker
from shared.compute_policy import ComputePoolRecord
from shared.errors import ConflictError
from shared.scheduling import (
    SchedulerWorkerRecord,
    SchedulerWorkerStatus,
)
from shared.source_cache_cleanup import (
    SourceCacheCleanupCompletionReason,
    WorkerCacheGenerationState,
)
from shared.timestamps import utc_now
from sqlalchemy import event
from sqlalchemy.orm import Session


class _SimulatedProcessCrash(BaseException):
    pass


@dataclass(slots=True)
class _RemoteMachine:
    provider_instance_id: str
    machine_id: str
    pool_name: str
    storage_volume_ids: tuple[str, ...]


@dataclass(slots=True)
class _DirectProvider:
    """Identity-reconciling direct provider fake with a remote machine view."""

    name: str
    offers: list[ComputeOffer]
    fail_after_launches: int | None = None
    launch_failure_detail: str | None = None
    fail_terminate: bool = False
    defer_storage_destruction: bool = False
    crash_after_launch: bool = False
    raise_after_launch: bool = False
    remote: dict[str, _RemoteMachine] = field(default_factory=dict)
    launch_calls: list[str] = field(default_factory=list)
    registration_tokens: list[str] = field(default_factory=list)
    launch_requests: list[DirectMachineLaunchRequest] = field(default_factory=list)
    terminate_calls: list[str] = field(default_factory=list)
    operation_instances: dict[str, str] = field(default_factory=dict)
    operation_tokens: dict[str, str] = field(default_factory=dict)
    hide_remote_reconciliations: int = 0

    def list_offers(self) -> list[ComputeOffer]:
        return [offer.model_copy(update={"provider": self.name}) for offer in self.offers]

    def launch_machine(self, request: DirectMachineLaunchRequest) -> ProviderMachineReference:
        if self.fail_after_launches is not None and (
            len(self.launch_calls) >= self.fail_after_launches
        ):
            raise RuntimeError(
                self.launch_failure_detail or f"provider {self.name} launch capacity exhausted"
            )
        # Mirrors EC2 ClientToken semantics: a replay under the same idempotency key
        # must carry an identical payload, and a new key is a new launch.
        prior_token = self.operation_tokens.get(request.idempotency_key)
        if prior_token is not None and prior_token != request.registration_token:
            raise RuntimeError("idempotent launch payload changed")
        instance_id = self.operation_instances.get(
            request.operation_id, f"i-{self.name}-{len(self.operation_instances)}"
        )
        self.launch_calls.append(instance_id)
        self.registration_tokens.append(request.registration_token)
        self.launch_requests.append(request)
        self.operation_instances[request.operation_id] = instance_id
        self.operation_tokens[request.idempotency_key] = request.registration_token
        self.remote[instance_id] = _RemoteMachine(
            provider_instance_id=instance_id,
            machine_id=request.machine_id,
            pool_name=request.pool_name,
            storage_volume_ids=(f"vol-{instance_id}",),
        )
        if self.crash_after_launch:
            raise _SimulatedProcessCrash
        if self.raise_after_launch:
            raise RuntimeError(f"provider {self.name} lost launch response")
        return ProviderMachineReference(
            provider_instance_id=instance_id,
            machine_id=request.machine_id,
            name=f"{self.name}-{request.machine_id}",
            status=ProviderMachineStatus.Active,
            address="10.0.0.9",
            storage_volume_ids=(f"vol-{instance_id}",),
        )

    def add_orphan(self, pool_name: str, machine_id: str) -> str:
        instance_id = f"i-{self.name}-orphan-{machine_id}"
        self.remote[instance_id] = _RemoteMachine(
            provider_instance_id=instance_id,
            machine_id=machine_id,
            pool_name=pool_name,
            storage_volume_ids=(f"vol-{instance_id}",),
        )
        return instance_id

    def reconcile_machines(
        self,
        pool_name: str,
        expected_machine_ids: set[str],
        *,
        terminate_stale: bool = False,
    ) -> ProviderReconcileResult:
        if self.hide_remote_reconciliations > 0:
            self.hide_remote_reconciliations -= 1
            machines: dict[str, _RemoteMachine] = {}
        else:
            machines = {
                machine.machine_id: machine
                for machine in self.remote.values()
                if machine.pool_name == pool_name
            }
        stale = sorted(set(machines) - expected_machine_ids)
        terminated: list[str] = []
        if terminate_stale:
            for machine_id in stale:
                self.terminate_machine(machines[machine_id].provider_instance_id)
                if self.machine_storage_destroyed(
                    machines[machine_id].provider_instance_id,
                    machines[machine_id].storage_volume_ids,
                ):
                    terminated.append(machine_id)
        return ProviderReconcileResult(
            observed_machines=[
                ProviderMachineReference(
                    provider_instance_id=machine.provider_instance_id,
                    machine_id=machine.machine_id,
                    storage_volume_ids=machine.storage_volume_ids,
                )
                for machine in machines.values()
            ],
            missing_machine_ids=sorted(expected_machine_ids - set(machines)),
            stale_machine_ids=stale,
            terminated_machine_ids=terminated,
        )

    def terminate_machine(self, provider_instance_id: str, /) -> None:
        self.terminate_calls.append(provider_instance_id)
        if self.fail_terminate:
            raise RuntimeError(f"provider {self.name} terminate failed")
        if not self.defer_storage_destruction:
            self.remote.pop(provider_instance_id, None)

    def machine_storage_destroyed(
        self,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
        /,
    ) -> bool:
        if not storage_volume_ids:
            return False
        return provider_instance_id not in self.remote

    def confirm_storage_destroyed(self, provider_instance_id: str) -> None:
        self.remote.pop(provider_instance_id, None)


@dataclass(slots=True)
class _Registry:
    providers: dict[str, _DirectProvider]

    def snapshot(self, workspace: str) -> MappingProxyType[str, _DirectProvider]:
        del workspace
        return MappingProxyType(dict(sorted(self.providers.items())))

    def snapshot_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> MappingProxyType[str, _DirectProvider]:
        del workspace_id
        return MappingProxyType(dict(sorted(self.providers.items())))


@dataclass(slots=True)
class _RecordingBilling:
    launch_requests: list[BillingCreditRequest] = field(default_factory=list)

    def check_launch_credit(self, request: BillingCreditRequest) -> BillingDecision:
        self.launch_requests.append(request)
        return BillingDecision(ok=True)

    def check_balance(self, workspace_id: str) -> BillingDecision:
        del workspace_id
        return BillingDecision(ok=True)

    def record_usage(self, usage: ManagedUsage) -> None:
        del usage


@dataclass(slots=True)
class _RecordingSchedulerHooks:
    registered_pools: list[str] = field(default_factory=list)
    available_machines: set[str] = field(default_factory=set)
    registered_machines: list[str] = field(default_factory=list)
    disabled_machines: list[str] = field(default_factory=list)

    def register_pool(self, state: PrivatePoolState) -> None:
        self.registered_pools.append(state.name)

    def register_machine(self, machine: Machine) -> None:
        self.registered_machines.append(machine.id)

    def register_internal_pool(self, pool: ComputePoolRecord, offer: ComputeOffer) -> None:
        del pool, offer

    def disable_machine(self, machine_id: str, reason: str) -> None:
        del reason
        self.disabled_machines.append(machine_id)

    def machine_worker_available(self, machine_id: str) -> bool:
        return machine_id in self.available_machines

    def retire_machine(
        self,
        workspace_id: str,
        pool_name: str,
        machine_id: str,
        reason: str,
    ) -> None:
        del workspace_id, pool_name, machine_id, reason

    def revoke_pool_join_token(self, token_hash: str) -> None:
        del token_hash


def _phase_deadlines(seconds: int) -> dict[str, int]:
    return dict.fromkeys(("requested", "provisioning", "booting", "joining"), seconds)


def _scheduler_worker_available(services: ApiServices, machine_id: str, *, pool: str) -> None:
    """Make the scheduler's hot record say this machine's worker takes work.

    Readiness is decided by that record, not by the durable `Worker` row — a row
    that says `Running` from the moment of registration.
    """
    hooks = services.compute.scheduler_hooks
    assert isinstance(hooks, SchedulerComputeHooks)
    workers = hooks.workers
    assert isinstance(workers, RedisSchedulerWorkerRepository)
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=agent_machine_worker_id(machine_id),
            pool_name=pool,
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            machine_id=machine_id,
            status=SchedulerWorkerStatus.Available,
        )
    )


def _offer(provider: str, *, hourly_cost_micros: int, available: int = 4) -> ComputeOffer:
    return ComputeOffer(
        id=f"{provider}-cpu",
        provider=provider,
        instance_type="cpu-large",
        region="lab",
        cpu_millicores=4000,
        memory_mb=8192,
        hourly_cost_micros=hourly_cost_micros,
        available=available,
    )


def _install_providers(services: ApiServices, providers: dict[str, _DirectProvider]) -> None:
    services.compute.provider_registry = _Registry(providers)
    services.compute.provider_resolver = None


def test_direct_capacity_plan_is_read_only_and_retry_preserves_exact_intent(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    state = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="planned-operation-pool",
            providers=["alpha"],
            nodes=1,
            ttl="1h",
            max_spend=1.0,
        )
    )
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, state.name)
    assert pool is not None
    planning = CapacityAcquisitionPlanningRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=8_192),
    )
    launch_calls_before_plan = list(alpha.launch_calls)

    planned = isolated_services.compute.plan_capacity_acquisition(planning)

    assert planned.status is CapacityAcquisitionStatus.Requested
    assert planned.desired_unit == 2
    assert alpha.launch_calls == launch_calls_before_plan
    requested = isolated_services.compute.acquire_capacity(
        CapacityAcquisitionRequest(
            **planning.model_dump(),
            desired_unit=planned.desired_unit,
        )
    )
    launch_calls_after_acquire = list(alpha.launch_calls)

    retried_plan = isolated_services.compute.plan_capacity_acquisition(planning)

    assert requested.status is CapacityAcquisitionStatus.Requested
    assert retried_plan.status is CapacityAcquisitionStatus.Requested
    assert retried_plan.desired_unit == planned.desired_unit
    assert retried_plan.target_machine_id == requested.target_machine_id
    assert alpha.launch_calls == launch_calls_after_acquire


def test_direct_capacity_plan_rejects_limit_and_fixed_shape_without_mutation(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(
        name="alpha",
        offers=[_offer("alpha", hourly_cost_micros=100_000, available=1)],
    )
    _install_providers(isolated_services, {"alpha": alpha})
    state = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="bounded-operation-pool",
            providers=["alpha"],
            nodes=1,
            ttl="1h",
            max_spend=1.0,
        )
    )
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, state.name)
    assert pool is not None
    request_fields = {
        "capacity_owner_id": pool.capacity_owner_id,
        "reservation_id": str(uuid4()),
        "operation_id": str(uuid4()),
    }
    launch_calls_before_plan = list(alpha.launch_calls)

    at_limit = isolated_services.compute.plan_capacity_acquisition(
        CapacityAcquisitionPlanningRequest(
            **request_fields,
            shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=8_192),
        )
    )
    fixed_shape_rejection = isolated_services.compute.plan_capacity_acquisition(
        CapacityAcquisitionPlanningRequest(
            **{
                **request_fields,
                "reservation_id": str(uuid4()),
                "operation_id": str(uuid4()),
            },
            shape=CapacityAcquisitionShape(cpu_millicores=8_000, memory_mib=8_192),
        )
    )

    assert at_limit.status is CapacityAcquisitionStatus.AtLimit
    assert at_limit.desired_unit == 1
    assert fixed_shape_rejection.status is CapacityAcquisitionStatus.Unsupported
    assert "fixed worker shape" in fixed_shape_rejection.reason
    assert alpha.launch_calls == launch_calls_before_plan


def test_direct_capacity_acquisition_retries_without_launching_a_second_machine(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    state = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="operation-pool",
            providers=["alpha"],
            nodes=1,
            ttl="1h",
            max_spend=1.0,
        )
    )
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, state.name)
    assert pool is not None
    acquisition = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        desired_unit=2,
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=8_192),
    )

    requested = isolated_services.compute.acquire_capacity(acquisition)
    retried = isolated_services.compute.acquire_capacity(acquisition)

    assert requested.status is CapacityAcquisitionStatus.Requested
    assert retried.status is CapacityAcquisitionStatus.ExistingPending
    assert requested.target_machine_id == retried.target_machine_id
    assert len(alpha.remote) == 2
    assert len(alpha.launch_calls) == 2

    released = isolated_services.compute.release_acquired_capacity(
        CapacityReleaseRequest(
            capacity_owner_id=acquisition.capacity_owner_id,
            reservation_id=acquisition.reservation_id,
            operation_id=acquisition.operation_id,
        )
    )

    assert released.status is CapacityAcquisitionStatus.Requested
    assert len(alpha.remote) == 1
    with isolated_services.context.database.session() as session:
        [operation] = ComputeCapacityOperationRepository(session).list_for_owner(
            pool.capacity_owner_id
        )
    assert operation.status == "released"


def test_direct_capacity_acquisition_recovers_a_lost_provider_response(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    state = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="lost-response-operation-pool",
            providers=["alpha"],
            nodes=1,
            ttl="1h",
            max_spend=1.0,
        )
    )
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, state.name)
    assert pool is not None
    acquisition = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        desired_unit=2,
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=8_192),
    )
    alpha.raise_after_launch = True

    unavailable = isolated_services.compute.acquire_capacity(acquisition)
    alpha.hide_remote_reconciliations = 1
    alpha.raise_after_launch = False
    recovered = isolated_services.compute.acquire_capacity(acquisition)

    assert unavailable.status is CapacityAcquisitionStatus.TemporarilyUnavailable
    assert recovered.status is CapacityAcquisitionStatus.Requested
    assert unavailable.target_machine_id == recovered.target_machine_id
    assert len(alpha.remote) == 2
    assert len(alpha.launch_calls) == 3
    assert alpha.registration_tokens[-1] == alpha.registration_tokens[-2]
    assert alpha.launch_requests[-1].model_dump() == alpha.launch_requests[-2].model_dump()
    with isolated_services.context.database.session() as session:
        credentials = ComputeJoinCredentialRepository(session).list_for_pool(
            pool.workspace_id,
            pool.name,
        )
    capacity_credentials = [
        item for item in credentials if item.machine_id == unavailable.target_machine_id
    ]
    assert len(capacity_credentials) == 1
    assert capacity_credentials[0].status is ComputeCredentialStatus.Active


def test_repeated_launch_uses_one_aggregate_capacity_request_and_deadline(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    billing = _RecordingBilling()
    isolated_services.compute.billing = billing
    isolated_services.compute.reclaim = ComputeReclaimPolicy(
        bootstrap_phase_deadline_seconds=_phase_deadlines(24 * 60 * 60)
    )
    started_at = utc_now()

    initial = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="aggregate-pool",
            providers=["alpha"],
            nodes=1,
            ttl="1h",
            max_spend=1.0,
        ),
        now=started_at,
    )
    isolated_services.compute.reconcile_provider_capacity(now=started_at + timedelta(minutes=30))
    aggregate = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="aggregate-pool",
            providers=["alpha"],
            nodes=1,
            ttl="30m",
            max_spend=2.0,
        ),
        now=started_at + timedelta(minutes=30),
    )

    assert initial.reserved_nodes == 1
    assert aggregate.reserved_nodes == 2
    assert aggregate.config is not None
    assert aggregate.config.max_spend == 3.0
    assert aggregate.expires_at == started_at + timedelta(hours=1)
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(
            aggregate.workspace_id,
            "aggregate-pool",
        )
        assert pool is not None
        capacity_requests = ComputeCapacityRequestRepository(session).list_for_pool(pool.id)
        instances = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        credentials = ComputeJoinCredentialRepository(session).list_for_pool(
            aggregate.workspace_id,
            "aggregate-pool",
        )

    assert len(capacity_requests) == 1
    [capacity] = capacity_requests
    assert capacity.status == "active"
    assert capacity.max_spend_micros == 3_000_000
    assert capacity.expires_at == started_at + timedelta(hours=1)
    assert {item.capacity_request_id for item in instances} == {capacity.id}
    assert {item.expires_at for item in instances} == {capacity.expires_at}
    assert {item.committed_micros for item in instances} == {100_000}
    assert [request.estimated_committed_micros for request in billing.launch_requests] == [
        100_000,
        200_000,
    ]
    credential_hashes = {item.token_hash for item in credentials}
    assert credential_hashes == {hash_compute_token(item) for item in alpha.registration_tokens}
    assert {item.machine_id for item in credentials} == {
        item.machine_id for item in instances if item.machine_id is not None
    }
    assert {
        str(item.metadata["registration_token_hash"]) for item in instances
    } == credential_hashes
    assert credential_hashes.isdisjoint(alpha.registration_tokens)

    isolated_services.gateway_service.pool_state_coordinator.create_pool_join_token(
        "aggregate-pool",
        workspace_id=aggregate.workspace_id,
        owner_token_id="workspace-cli",
    )
    with isolated_services.context.database.session() as session:
        rotated_credentials = ComputeJoinCredentialRepository(session).list_for_pool(
            aggregate.workspace_id,
            "aggregate-pool",
        )
    assert {
        item.status
        for item in rotated_credentials
        if item.machine_id in {credential.machine_id for credential in credentials}
    } == {ComputeCredentialStatus.Active}
    assert len([item for item in rotated_credentials if item.machine_id == ""]) == 1

    gateway = replace(
        isolated_services.gateway_service,
        tailnet=TailnetConfig(),
    )
    joined = gateway.join_agent(
        JoinAgentRequest(
            join_token=alpha.registration_tokens[0],
            machine_fingerprint="aggregate-provider-machine",
            hostname="aggregate-provider-machine",
            os="linux",
            arch="amd64",
            cpu_count=4,
            cpu_millicores=4000,
            memory_mb=8192,
            schedulable=False,
        )
    )
    expected_machine_id = next(
        item.machine_id
        for item in credentials
        if item.token_hash == hash_compute_token(alpha.registration_tokens[0])
    )
    assert joined.machine_id == expected_machine_id


def test_capacity_extension_only_moves_the_aggregate_deadline_forward(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    isolated_services.compute.reclaim = ComputeReclaimPolicy(
        bootstrap_phase_deadline_seconds=_phase_deadlines(24 * 60 * 60)
    )
    started_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    launched = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="extend-pool",
            providers=["alpha"],
            nodes=2,
            ttl="1h",
            max_spend=0.2,
        ),
        now=started_at,
    )
    extension_time = started_at + timedelta(minutes=15)
    isolated_services.compute.reconcile_provider_capacity(now=extension_time)

    with pytest.raises(ConflictError, match="deadline may only be extended"):
        isolated_services.compute.extend_pool_capacity(
            "extend-pool",
            ttl="45m",
            max_spend=0.2,
            now=extension_time,
        )
    with pytest.raises(ConflictError, match="projected commitment"):
        isolated_services.compute.extend_pool_capacity(
            "extend-pool",
            ttl="2h",
            max_spend=0.5,
            now=extension_time,
        )

    with isolated_services.context.database.session() as session:
        before_pool = ComputePoolRepository(session).get_by_name(
            launched.workspace_id,
            "extend-pool",
        )
        assert before_pool is not None
        before_capacity = ComputeCapacityRequestRepository(session).active_for_pool(before_pool.id)
        before_instances = ComputeProviderInstanceRepository(session).list_for_pool(before_pool.id)
    assert before_capacity is not None
    assert before_capacity.expires_at == started_at + timedelta(hours=1)
    assert {item.committed_micros for item in before_instances} == {100_000}

    extended = isolated_services.compute.extend_pool_capacity(
        "extend-pool",
        ttl="2h",
        max_spend=0.6,
        now=extension_time,
    )
    expected_deadline = extension_time + timedelta(hours=2)
    assert extended.expires_at == expected_deadline
    assert extended.config is not None
    assert extended.config.max_spend == 0.6
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(extended.workspace_id, "extend-pool")
        assert pool is not None
        capacity = ComputeCapacityRequestRepository(session).active_for_pool(pool.id)
        instances = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)

    assert capacity is not None
    assert capacity.expires_at == expected_deadline
    assert {item.expires_at for item in instances} == {expected_deadline}
    assert {item.committed_micros for item in instances} == {300_000}

    isolated_services.compute.reconcile_provider_capacity(now=expected_deadline)
    assert set(alpha.terminate_calls) == set(alpha.launch_calls)
    with isolated_services.context.database.session() as session:
        requests = ComputeCapacityRequestRepository(session).list_for_pool(pool.id)
        credentials = ComputeJoinCredentialRepository(session).list_for_pool(
            extended.workspace_id,
            "extend-pool",
        )
    assert [item.status for item in requests] == ["expired"]
    assert {item.status for item in credentials} == {ComputeCredentialStatus.Revoked}


def test_repeated_launch_prices_existing_and_new_nodes_over_aggregate_horizon(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    billing = _RecordingBilling()
    isolated_services.compute.billing = billing
    isolated_services.compute.reclaim = ComputeReclaimPolicy(
        bootstrap_phase_deadline_seconds=_phase_deadlines(24 * 60 * 60)
    )
    started_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    initial = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="aggregate-horizon",
            providers=["alpha"],
            nodes=1,
            ttl="1h",
            max_spend=0.1,
        ),
        now=started_at,
    )
    isolated_services.compute.reconcile_provider_capacity(now=started_at + timedelta(minutes=30))

    with pytest.raises(ConflictError, match="max spend would be exceeded"):
        isolated_services.compute.launch_pool_capacity(
            PoolConfig(
                name="aggregate-horizon",
                providers=["alpha"],
                nodes=1,
                ttl="2h",
                max_spend=0.29,
            ),
            now=started_at + timedelta(minutes=30),
        )
    unchanged = isolated_services.compute.get_private_pool_state("aggregate-horizon")
    assert unchanged is not None
    assert unchanged.expires_at == initial.expires_at
    assert unchanged.reserved_nodes == 1

    aggregate = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="aggregate-horizon",
            providers=["alpha"],
            nodes=1,
            ttl="2h",
            max_spend=0.4,
        ),
        now=started_at + timedelta(minutes=30),
    )

    assert aggregate.expires_at == started_at + timedelta(hours=2, minutes=30)
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(
            aggregate.workspace_id,
            "aggregate-horizon",
        )
        assert pool is not None
        capacity = ComputeCapacityRequestRepository(session).active_for_pool(pool.id)
        instances = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert capacity is not None
    assert capacity.max_spend_micros == 500_000
    assert sorted(item.committed_micros for item in instances) == [200_000, 300_000]
    assert [request.estimated_committed_micros for request in billing.launch_requests] == [
        100_000,
        500_000,
    ]


@pytest.mark.parametrize(
    ("ttl", "max_spend", "expected_commitment"),
    [("2h", 0.2, 200_000), ("7201", 0.3, 300_000)],
)
def test_capacity_extension_uses_whole_hour_boundaries(
    isolated_services: ApiServices,
    ttl: str,
    max_spend: float,
    expected_commitment: int,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    started_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    launched = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name=f"hour-boundary-{expected_commitment}",
            providers=["alpha"],
            nodes=1,
            ttl="1h",
            max_spend=0.1,
        ),
        now=started_at,
    )

    extended = isolated_services.compute.extend_pool_capacity(
        launched.name,
        ttl=ttl,
        max_spend=max_spend,
        now=started_at,
    )

    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(extended.workspace_id, extended.name)
        assert pool is not None
        [record] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert record.committed_micros == expected_commitment


def test_capacity_extension_rejects_cap_below_recorded_pool_spend(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    started_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    launched = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="recorded-spend",
            providers=["alpha"],
            nodes=1,
            ttl="1h",
            max_spend=0.1,
        ),
        now=started_at,
    )
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(launched.workspace_id, launched.name)
        assert pool is not None
        [record] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        ComputeLedgerRepository(session).append(
            ComputeLedgerRecord(
                id=str(uuid4()),
                workspace_id=launched.workspace_id,
                pool_id=pool.id,
                reservation_id=record.id,
                source="managed_compute",
                amount_micros=250_000,
                started_at=started_at,
                ended_at=started_at + timedelta(minutes=30),
            )
        )

    with pytest.raises(ConflictError, match="recorded spend"):
        isolated_services.compute.extend_pool_capacity(
            launched.name,
            ttl="2h",
            max_spend=0.2,
            now=started_at,
        )
    extended = isolated_services.compute.extend_pool_capacity(
        launched.name,
        ttl="2h",
        max_spend=0.25,
        now=started_at,
    )
    assert extended.config is not None
    assert extended.config.max_spend == 0.25


def test_capacity_reconciliation_repairs_commitment_without_renewing_past_deadline(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    started_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    launched = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="reconcile-deadline",
            providers=["alpha"],
            nodes=1,
            ttl="4h",
            max_spend=0.4,
        ),
        now=started_at,
    )
    deadline = started_at + timedelta(hours=4)
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(launched.workspace_id, launched.name)
        assert pool is not None
        repository = ComputeProviderInstanceRepository(session)
        [record] = repository.list_for_pool(pool.id)
        assert record.machine_id is not None
        ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                workspace_id=launched.workspace_id,
                pool_name=launched.name,
                machine_id=record.machine_id,
                machine_fingerprint_hash="a" * 64,
                credential_hash="b" * 64,
                status=ComputeMachineEnrollmentStatus.Active,
                heartbeat_confirmed=True,
                schedulable=True,
                readiness_phase=MachineReadinessPhase.Ready,
                last_join_at=started_at,
                last_heartbeat_at=started_at,
            )
        )
        WorkerRepository(session).upsert(
            Worker(
                id=agent_machine_worker_id(record.machine_id),
                machine_id=record.machine_id,
                pool=launched.name,
                status=ResourceStatus.Running,
                last_seen_at=started_at,
            ),
            workspace_id=launched.workspace_id,
        )
        repository.upsert(
            record.model_copy(
                update={
                    "committed_micros": 100_000,
                    "expires_at": started_at + timedelta(hours=2),
                }
            )
        )

    isolated_services.compute.reconcile_provider_capacity(now=started_at + timedelta(minutes=30))
    with isolated_services.context.database.session() as session:
        [repaired] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert repaired.expires_at == deadline
    assert repaired.committed_micros == 400_000
    assert alpha.terminate_calls == []

    isolated_services.compute.reconcile_provider_capacity(now=deadline - timedelta(minutes=1))
    with isolated_services.context.database.session() as session:
        [near_deadline] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert near_deadline.expires_at == deadline
    assert near_deadline.committed_micros == 400_000
    assert alpha.terminate_calls == []


def test_capacity_reconciliation_fails_closed_when_durable_cap_is_undercommitted(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    started_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    launched = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="undercommitted",
            providers=["alpha"],
            nodes=1,
            ttl="4h",
            max_spend=0.4,
        ),
        now=started_at,
    )
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(launched.workspace_id, launched.name)
        assert pool is not None
        capacities = ComputeCapacityRequestRepository(session)
        capacity = capacities.active_for_pool(pool.id)
        assert capacity is not None
        capacities.upsert(capacity.model_copy(update={"max_spend_micros": 300_000}))

    reconciled = isolated_services.compute.reconcile_provider_capacity(
        now=started_at + timedelta(minutes=30)
    )
    [state] = [item for item in reconciled if item.name == launched.name]
    assert [record.status for record in state.reservations] == ["deleted"]
    assert alpha.terminate_calls == alpha.launch_calls


def test_expired_capacity_retries_provider_termination_without_renewing(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(
        name="alpha",
        offers=[_offer("alpha", hourly_cost_micros=100_000)],
        fail_terminate=True,
    )
    _install_providers(isolated_services, {"alpha": alpha})
    isolated_services.compute.reclaim = ComputeReclaimPolicy(
        bootstrap_phase_deadline_seconds=_phase_deadlines(24 * 60 * 60)
    )
    started_at = utc_now()
    launched = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="termination-retry-pool",
            providers=["alpha"],
            nodes=1,
            ttl="30m",
            max_spend=1.0,
        ),
        now=started_at,
    )
    deadline = started_at + timedelta(minutes=30)

    first = isolated_services.compute.reconcile_provider_capacity(now=deadline)
    [first_state] = [item for item in first if item.name == launched.name]
    assert [item.status for item in first_state.reservations] == ["terminating"]

    alpha.fail_terminate = False
    second = isolated_services.compute.reconcile_provider_capacity(
        now=deadline + timedelta(minutes=1)
    )
    [second_state] = [item for item in second if item.name == launched.name]
    assert [item.status for item in second_state.reservations] == ["deleted"]
    assert alpha.terminate_calls == [alpha.launch_calls[0], alpha.launch_calls[0]]
    assert second_state.reservations[0].expires_at == deadline


def test_machine_cache_retires_only_after_provider_proves_storage_destroyed(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(
        name="alpha",
        offers=[_offer("alpha", hourly_cost_micros=100_000)],
        defer_storage_destruction=True,
    )
    _install_providers(isolated_services, {"alpha": alpha})
    started_at = utc_now()
    launched = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="cache-proof-pool",
            providers=["alpha"],
            nodes=1,
            ttl="1h",
            max_spend=1.0,
        ),
        now=started_at,
    )
    [reservation] = launched.reservations
    machine_id = reservation.machine_id
    assert machine_id
    generation_id = str(uuid4())
    source_object_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        repository = SourceCacheCleanupRepository(session)
        repository.register_generation(
            generation_id,
            worker_id=f"worker-{machine_id}",
            storage_id=f"machine:{machine_id}",
            workspace_id=None,
            now=started_at,
        )
        repository.add_targets(
            workspace_id=launched.workspace_id,
            source_object_ids=[source_object_id],
            now=started_at,
        )

    requested = isolated_services.compute.terminate_pool_machine(
        launched.name,
        machine_id,
    )

    [requested_reservation] = requested.reservations
    assert requested_reservation.status == "terminating"
    assert requested_reservation.instance_id in alpha.remote
    with isolated_services.context.database.session() as session:
        generation = SourceCacheCleanupRepository(session).get_generation(generation_id)
        machine = MachineRepository(session).get_across_workspaces(machine_id)
    assert generation is not None
    assert generation.state is not WorkerCacheGenerationState.Retired
    assert machine is not None
    assert machine.status is not ResourceStatus.Deleted

    alpha.confirm_storage_destroyed(requested_reservation.instance_id)
    completed = isolated_services.compute.terminate_pool_machine(
        launched.name,
        machine_id,
    )

    [completed_reservation] = completed.reservations
    assert completed_reservation.status == "deleted"
    with isolated_services.context.database.session() as session:
        repository = SourceCacheCleanupRepository(session)
        generation = repository.get_generation(generation_id)
        [target] = repository.list_targets(generation_ids=[generation_id])
        machine = MachineRepository(session).get_across_workspaces(machine_id)
        pool = ComputePoolRepository(session).get_by_name(
            launched.workspace_id,
            launched.name,
        )
        assert pool is not None
        [provider_record] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert generation is not None
    assert generation.state is WorkerCacheGenerationState.Retired
    assert generation.storage_destroyed_at is not None
    assert target.completion_reason is SourceCacheCleanupCompletionReason.StorageDestroyed
    assert provider_record.metadata["provider_storage_destroyed_at"]
    assert machine is not None
    assert machine.status is ResourceStatus.Deleted


def test_failed_launch_compensates_only_the_owning_provider(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(
        name="alpha",
        offers=[_offer("alpha", hourly_cost_micros=100_000)],
        fail_after_launches=1,
        launch_failure_detail="provider request included must-not-persist",
    )
    beta = _DirectProvider(name="beta", offers=[_offer("beta", hourly_cost_micros=900_000)])
    _install_providers(isolated_services, {"alpha": alpha, "beta": beta})
    started_at = utc_now()

    with pytest.raises(RuntimeError, match="must-not-persist"):
        isolated_services.compute.launch_pool_capacity(
            PoolConfig(
                name="comp-pool",
                providers=["alpha", "beta"],
                nodes=3,
                ttl="1h",
                max_spend=5.0,
            ),
            now=started_at,
        )

    assert alpha.launch_calls == ["i-alpha-0"]
    assert alpha.terminate_calls == ["i-alpha-0"]
    assert beta.terminate_calls == []
    assert alpha.remote == {}
    state = isolated_services.compute.get_private_pool_state("comp-pool")
    assert state is not None
    assert {item.status for item in state.reservations} == {"deleted", "pending", "failed"}
    assert {item.last_error for item in state.reservations} == {
        "provider launch failed (RuntimeError)"
    }
    assert all("must-not-persist" not in item.last_error for item in state.reservations)
    with isolated_services.context.database.session() as session:
        credentials = ComputeJoinCredentialRepository(session).list_for_pool(
            state.workspace_id,
            "comp-pool",
        )
    assert [item.status for item in credentials].count(ComputeCredentialStatus.Active) == 1
    assert [item.status for item in credentials].count(ComputeCredentialStatus.Revoked) == 2
    isolated_services.compute.reconcile_provider_capacity(now=started_at + timedelta(seconds=31))
    state = isolated_services.compute.get_private_pool_state("comp-pool")
    assert state is not None
    assert {item.status for item in state.reservations} == {"deleted", "failed"}
    with isolated_services.context.database.session() as session:
        credentials = ComputeJoinCredentialRepository(session).list_for_pool(
            state.workspace_id,
            "comp-pool",
        )
    assert {item.status for item in credentials} == {ComputeCredentialStatus.Revoked}
    assert {
        machine.status
        for machine in isolated_services.compute.list_machines()
        if machine.pool == "comp-pool"
    } == {ResourceStatus.Failed}


def test_failed_launch_compensation_failure_is_surfaced(
    isolated_services: ApiServices,
    caplog: pytest.LogCaptureFixture,
) -> None:
    alpha = _DirectProvider(
        name="alpha",
        offers=[_offer("alpha", hourly_cost_micros=100_000)],
        fail_after_launches=1,
        fail_terminate=True,
    )
    _install_providers(isolated_services, {"alpha": alpha})

    with (
        caplog.at_level(logging.WARNING, logger="compute.service"),
        pytest.raises(RuntimeError, match="launch capacity exhausted"),
    ):
        isolated_services.compute.launch_pool_capacity(
            PoolConfig(
                name="comp-fail-pool",
                providers=["alpha"],
                nodes=2,
                ttl="1h",
                max_spend=5.0,
            )
        )

    assert alpha.terminate_calls == ["i-alpha-0"]
    surfaced = [
        record
        for record in caplog.records
        if record.message == "launch compensation failed to terminate provider machine"
    ]
    assert len(surfaced) == 1
    assert surfaced[0].__dict__["provider"] == "alpha"
    assert surfaced[0].__dict__["provider_instance_id"] == "i-alpha-0"
    assert surfaced[0].__dict__["machine_id"]


def test_post_launch_commit_failure_is_durable_and_fresh_reconciliation_finishes_cleanup(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(
        name="alpha",
        offers=[_offer("alpha", hourly_cost_micros=100_000)],
        fail_terminate=True,
    )
    _install_providers(isolated_services, {"alpha": alpha})
    hooks = _RecordingSchedulerHooks()
    isolated_services.compute.scheduler_hooks = hooks
    failed_commit = False

    def fail_first_post_launch_commit(session: Session) -> None:
        nonlocal failed_commit
        del session
        if alpha.launch_calls and not failed_commit:
            failed_commit = True
            raise RuntimeError("injected post-launch commit failure")

    session_type = isolated_services.database.sessions.class_
    event.listen(session_type, "before_commit", fail_first_post_launch_commit)
    try:
        with pytest.raises(RuntimeError, match="injected post-launch commit failure"):
            isolated_services.compute.launch_pool_capacity(
                PoolConfig(
                    name="commit-failure-pool",
                    providers=["alpha"],
                    nodes=1,
                    ttl="1h",
                    max_spend=5.0,
                )
            )
    finally:
        event.remove(session_type, "before_commit", fail_first_post_launch_commit)

    assert failed_commit
    assert alpha.launch_calls == ["i-alpha-0"]
    assert alpha.terminate_calls == ["i-alpha-0"]
    assert set(alpha.remote) == {"i-alpha-0"}
    assert hooks.registered_machines == []
    assert hooks.registered_pools == []

    with isolated_services.database.session() as session:
        workspace_id = isolated_services.context.workspace(session).id
        pool = ComputePoolRepository(session).get_by_name(
            workspace_id,
            "commit-failure-pool",
        )
        assert pool is not None
        [provider_record] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        [credential] = ComputeJoinCredentialRepository(session).list_for_pool(
            workspace_id,
            "commit-failure-pool",
        )
        machine = MachineRepository(session).get_across_workspaces(provider_record.machine_id or "")
        assert machine is not None
    assert provider_record.instance_id == "i-alpha-0"
    assert provider_record.status == "terminating"
    assert credential.status is ComputeCredentialStatus.Revoked
    assert machine.status is ResourceStatus.Created

    alpha.fail_terminate = False
    fresh_compute = replace(isolated_services.compute, scheduler_hooks=None)
    fresh_compute.reconcile_provider_capacity(now=utc_now())

    assert alpha.terminate_calls == ["i-alpha-0", "i-alpha-0"]
    assert alpha.remote == {}
    with isolated_services.database.session() as session:
        [cleaned] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        [credential] = ComputeJoinCredentialRepository(session).list_for_pool(
            workspace_id,
            "commit-failure-pool",
        )
    assert cleaned.status == "deleted"
    assert credential.status is ComputeCredentialStatus.Revoked


def test_crash_after_provider_create_is_discovered_bound_and_reclaimed(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(
        name="alpha",
        offers=[_offer("alpha", hourly_cost_micros=100_000)],
        crash_after_launch=True,
    )
    _install_providers(isolated_services, {"alpha": alpha})
    started_at = utc_now()

    with pytest.raises(_SimulatedProcessCrash):
        isolated_services.compute.launch_pool_capacity(
            PoolConfig(
                name="crashed-launch-pool",
                providers=["alpha"],
                nodes=1,
                ttl="1h",
                max_spend=5.0,
            ),
            now=started_at,
        )

    assert set(alpha.remote) == {"i-alpha-0"}
    with isolated_services.database.session() as session:
        workspace_id = isolated_services.context.workspace(session).id
        pool = ComputePoolRepository(session).get_by_name(workspace_id, "crashed-launch-pool")
        assert pool is not None
        [intent] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert intent.instance_id == f"intent:{intent.machine_id}"
    assert intent.status == "pending"

    alpha.crash_after_launch = False
    fresh_compute = replace(
        isolated_services.compute,
        scheduler_hooks=None,
        reclaim=ComputeReclaimPolicy(
            launch_intent_settle_seconds=30,
            bootstrap_phase_deadline_seconds=_phase_deadlines(3600),
        ),
    )
    fresh_compute.reconcile_provider_capacity(now=started_at + timedelta(seconds=29))

    assert set(alpha.remote) == {"i-alpha-0"}
    with isolated_services.database.session() as session:
        [settling] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert settling.status == "pending"

    fresh_compute.reconcile_provider_capacity(now=started_at + timedelta(seconds=31))

    assert alpha.terminate_calls == ["i-alpha-0"]
    assert alpha.remote == {}
    with isolated_services.database.session() as session:
        [cleaned] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        [credential] = ComputeJoinCredentialRepository(session).list_for_pool(
            workspace_id,
            "crashed-launch-pool",
        )
    assert cleaned.instance_id == "i-alpha-0"
    assert cleaned.status == "deleted"
    assert credential.status is ComputeCredentialStatus.Revoked


def test_lost_provider_launch_response_is_discovered_and_reclaimed(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(
        name="alpha",
        offers=[_offer("alpha", hourly_cost_micros=100_000)],
        raise_after_launch=True,
    )
    _install_providers(isolated_services, {"alpha": alpha})
    started_at = utc_now()

    with pytest.raises(RuntimeError, match="lost launch response"):
        isolated_services.compute.launch_pool_capacity(
            PoolConfig(
                name="lost-response-pool",
                providers=["alpha"],
                nodes=1,
                ttl="1h",
                max_spend=5.0,
            ),
            now=started_at,
        )

    assert set(alpha.remote) == {"i-alpha-0"}
    assert alpha.terminate_calls == []
    with isolated_services.database.session() as session:
        workspace_id = isolated_services.context.workspace(session).id
        pool = ComputePoolRepository(session).get_by_name(workspace_id, "lost-response-pool")
        assert pool is not None
        [intent] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        [credential] = ComputeJoinCredentialRepository(session).list_for_pool(
            workspace_id,
            "lost-response-pool",
        )
        machine = MachineRepository(session).get_across_workspaces(intent.machine_id or "")
        assert machine is not None
    assert intent.instance_id == f"intent:{intent.machine_id}"
    assert intent.status == "pending"
    assert intent.metadata["launch_state"] == "intent"
    assert intent.metadata["launch_outcome"] == "unknown"
    assert credential.status is ComputeCredentialStatus.Active
    assert machine.status is not ResourceStatus.Deleted

    alpha.raise_after_launch = False
    fresh_compute = replace(
        isolated_services.compute,
        scheduler_hooks=None,
        reclaim=ComputeReclaimPolicy(
            launch_intent_settle_seconds=30,
            bootstrap_phase_deadline_seconds=_phase_deadlines(3600),
        ),
    )
    fresh_compute.reconcile_provider_capacity(now=started_at + timedelta(seconds=31))

    assert alpha.terminate_calls == ["i-alpha-0"]
    assert alpha.remote == {}
    with isolated_services.database.session() as session:
        [cleaned] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        [credential] = ComputeJoinCredentialRepository(session).list_for_pool(
            workspace_id,
            "lost-response-pool",
        )
        machine = MachineRepository(session).get_across_workspaces(cleaned.machine_id or "")
        assert machine is not None
    assert cleaned.instance_id == "i-alpha-0"
    assert cleaned.status == "deleted"
    assert credential.status is ComputeCredentialStatus.Revoked
    assert machine.status is ResourceStatus.Deleted


def test_stale_machine_reclaim_honors_the_configured_grace_window(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    isolated_services.compute.reclaim = ComputeReclaimPolicy(
        stale_grace_seconds=600,
        bootstrap_phase_deadline_seconds=_phase_deadlines(24 * 60 * 60),
        provider_stale_grace_seconds={"alpha": 300},
    )
    state = isolated_services.compute.launch_pool_capacity(
        PoolConfig(name="stale-pool", providers=["alpha"], nodes=1, ttl="4h", max_spend=5.0)
    )
    expected_machine_id = state.reservations[0].machine_id
    orphan_instance_id = alpha.add_orphan("stale-pool", f"machine-{uuid4().hex[:8]}")

    first_seen = utc_now()
    isolated_services.compute.reconcile_provider_capacity(now=first_seen)
    assert orphan_instance_id in alpha.remote

    isolated_services.compute.reconcile_provider_capacity(now=first_seen + timedelta(seconds=299))
    assert orphan_instance_id in alpha.remote

    isolated_services.compute.reconcile_provider_capacity(now=first_seen + timedelta(seconds=300))
    assert orphan_instance_id not in alpha.remote
    assert alpha.terminate_calls == [orphan_instance_id]

    reconciled = isolated_services.compute.get_private_pool_state("stale-pool")
    assert reconciled is not None
    open_reservations = [
        reservation
        for reservation in reconciled.reservations
        if reservation.status not in {"deleted", "failed"}
    ]
    assert [reservation.machine_id for reservation in open_reservations] == [expected_machine_id]


def test_never_registered_machine_is_reclaimed_after_the_deadline(
    isolated_services: ApiServices,
) -> None:
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    isolated_services.compute.reclaim = ComputeReclaimPolicy(
        bootstrap_phase_deadline_seconds=_phase_deadlines(1800)
    )
    state = isolated_services.compute.launch_pool_capacity(
        PoolConfig(name="reclaim-pool", providers=["alpha"], nodes=3, ttl="4h", max_spend=9.0)
    )
    launched_at = utc_now()
    registered_machine_id = state.reservations[0].machine_id
    silent_machine_id = state.reservations[1].machine_id
    pending_worker_machine_id = state.reservations[2].machine_id
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, "reclaim-pool")
        assert pool is not None
        repository = ComputeProviderInstanceRepository(session)
        for record in repository.list_for_pool(pool.id):
            if record.machine_id in {registered_machine_id, pending_worker_machine_id}:
                repository.upsert(
                    record.model_copy(
                        update={
                            "bootstrap_phase": MachineBootstrapPhase.Joining,
                            "bootstrap_observed_at": launched_at,
                        }
                    )
                )
    silent_instance_id = next(
        reservation.instance_id
        for reservation in state.reservations
        if reservation.machine_id == silent_machine_id
    )
    pending_worker_instance_id = next(
        reservation.instance_id
        for reservation in state.reservations
        if reservation.machine_id == pending_worker_machine_id
    )
    with isolated_services.context.database.session() as session:
        ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                workspace_id=state.workspace_id,
                pool_name="reclaim-pool",
                machine_id=registered_machine_id,
                machine_fingerprint_hash="a" * 64,
                credential_hash="b" * 64,
                status=ComputeMachineEnrollmentStatus.Active,
                heartbeat_confirmed=True,
                schedulable=True,
                readiness_phase=MachineReadinessPhase.Ready,
                last_join_at=launched_at,
                last_heartbeat_at=launched_at,
            )
        )
        WorkerRepository(session).upsert(
            Worker(
                id=agent_machine_worker_id(registered_machine_id),
                machine_id=registered_machine_id,
                pool="reclaim-pool",
                status=ResourceStatus.Running,
                last_seen_at=launched_at,
            ),
            workspace_id=state.workspace_id,
        )
        ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                workspace_id=state.workspace_id,
                pool_name="reclaim-pool",
                machine_id=pending_worker_machine_id,
                machine_fingerprint_hash="c" * 64,
                credential_hash="d" * 64,
                status=ComputeMachineEnrollmentStatus.Active,
                heartbeat_confirmed=True,
                readiness_phase=MachineReadinessPhase.Joining,
                last_join_at=launched_at,
                last_heartbeat_at=launched_at,
            )
        )

    _scheduler_worker_available(
        isolated_services,
        registered_machine_id,
        pool="reclaim-pool",
    )
    before_deadline = launched_at + timedelta(seconds=1200)
    isolated_services.compute.reconcile_provider_capacity(now=before_deadline)
    assert alpha.terminate_calls == []

    past_deadline = launched_at + timedelta(seconds=1860)
    isolated_services.compute.reconcile_provider_capacity(now=past_deadline)
    assert set(alpha.terminate_calls) == {silent_instance_id, pending_worker_instance_id}

    reconciled = isolated_services.compute.get_private_pool_state("reclaim-pool")
    assert reconciled is not None
    by_machine = {
        reservation.machine_id: reservation.status for reservation in reconciled.reservations
    }
    assert by_machine[silent_machine_id] == "deleted"
    assert by_machine[pending_worker_machine_id] == "deleted"
    assert by_machine[registered_machine_id] not in {"deleted", "failed"}
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, "reclaim-pool")
        assert pool is not None
        records = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        reclaimed = next(record for record in records if record.machine_id == silent_machine_id)
        pending_worker = next(
            record for record in records if record.machine_id == pending_worker_machine_id
        )
    assert reclaimed.metadata["terminating_reason"] == "bootstrap_deadline_exceeded"
    assert reclaimed.bootstrap_phase.value == "failed"
    assert reclaimed.bootstrap_failure_reason is MachineBootstrapFailureReason.BootstrapTimedOut
    assert (
        pending_worker.bootstrap_failure_reason
        is MachineBootstrapFailureReason.WorkerReadinessFailed
    )
    machines = {
        machine.id: machine.status
        for machine in isolated_services.compute.list_machines()
        if machine.pool == "reclaim-pool"
    }
    assert registered_machine_id in machines
    assert machines.get(silent_machine_id, ResourceStatus.Deleted) is ResourceStatus.Deleted


def test_a_machine_that_reported_its_failure_is_reclaimed_under_that_reason(
    isolated_services: ApiServices,
) -> None:
    """Reporting a failure must not cost more than dying silently.

    `Failed` had no deadline, and `Failed -> Deleting` is the only transition
    out of it, so a machine that said why it failed ran and billed until someone
    noticed — while one that said nothing was reclaimed on the phase clock. The
    reported reason also has to survive the reclaim: re-labelling it
    `BootstrapTimedOut` discards the only diagnosis the node managed to send.
    """
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    # The shipped defaults, deliberately: the bound on `Failed` is the subject.
    isolated_services.compute.reclaim = ComputeReclaimPolicy()
    state = isolated_services.compute.launch_pool_capacity(
        PoolConfig(name="reclaim-pool", providers=["alpha"], nodes=1, ttl="4h", max_spend=9.0)
    )
    reported_at = utc_now()
    machine_id = state.reservations[0].machine_id
    instance_id = state.reservations[0].instance_id
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, "reclaim-pool")
        assert pool is not None
        repository = ComputeProviderInstanceRepository(session)
        for record in repository.list_for_pool(pool.id):
            if record.machine_id == machine_id:
                repository.upsert(
                    record.model_copy(
                        update={
                            "bootstrap_phase": MachineBootstrapPhase.Failed,
                            "bootstrap_failure_reason": (
                                MachineBootstrapFailureReason.NetworkJoinFailed
                            ),
                            "bootstrap_observed_at": reported_at,
                        }
                    )
                )

    isolated_services.compute.reconcile_provider_capacity(now=reported_at + timedelta(seconds=120))
    assert alpha.terminate_calls == []

    isolated_services.compute.reconcile_provider_capacity(now=reported_at + timedelta(seconds=360))

    assert alpha.terminate_calls == [instance_id]
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, "reclaim-pool")
        assert pool is not None
        record = next(
            item
            for item in ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
            if item.machine_id == machine_id
        )
    assert record.bootstrap_failure_reason is MachineBootstrapFailureReason.NetworkJoinFailed


def test_unreachable_worker_state_keeps_the_machine(
    isolated_services: ApiServices,
) -> None:
    """Reclaim terminates billable machines on this answer; an outage is not one.

    When the worker-state store cannot be read, the machine's readiness is
    unknown, and unknown machines are kept — never read as gone and terminated.
    """

    @dataclass(slots=True)
    class _UnreachableWorkerState(_RecordingSchedulerHooks):
        def machine_worker_available(self, machine_id: str) -> bool:
            raise ConnectionError("worker state store is unreachable")

    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    _install_providers(isolated_services, {"alpha": alpha})
    isolated_services.compute.reclaim = ComputeReclaimPolicy(
        bootstrap_phase_deadline_seconds=_phase_deadlines(300)
    )
    state = isolated_services.compute.launch_pool_capacity(
        PoolConfig(name="reclaim-pool", providers=["alpha"], nodes=1, ttl="4h", max_spend=9.0)
    )
    launched_at = utc_now()
    machine_id = state.reservations[0].machine_id
    assert machine_id is not None
    with isolated_services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, "reclaim-pool")
        assert pool is not None
        repository = ComputeProviderInstanceRepository(session)
        for record in repository.list_for_pool(pool.id):
            repository.upsert(
                record.model_copy(
                    update={
                        "bootstrap_phase": MachineBootstrapPhase.Joining,
                        "bootstrap_observed_at": launched_at,
                    }
                )
            )
        ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                workspace_id=state.workspace_id,
                pool_name="reclaim-pool",
                machine_id=machine_id,
                machine_fingerprint_hash="a" * 64,
                credential_hash="b" * 64,
                status=ComputeMachineEnrollmentStatus.Active,
                heartbeat_confirmed=True,
                schedulable=True,
                readiness_phase=MachineReadinessPhase.Ready,
                last_join_at=launched_at,
                last_heartbeat_at=launched_at,
            )
        )
    isolated_services.compute.scheduler_hooks = _UnreachableWorkerState()

    isolated_services.compute.reconcile_provider_capacity(now=launched_at + timedelta(seconds=600))

    assert alpha.terminate_calls == []


def _pool_for_capacity(
    services: ApiServices, alpha: _DirectProvider, name: str
) -> ComputePoolRecord:
    _install_providers(services, {"alpha": alpha})
    state = services.compute.launch_pool_capacity(
        PoolConfig(name=name, providers=["alpha"], nodes=1, ttl="1h", max_spend=1.0)
    )
    with services.context.database.session() as session:
        pool = ComputePoolRepository(session).get_by_name(state.workspace_id, state.name)
    assert pool is not None
    return pool


def _capacity_credentials(
    services: ApiServices,
    pool: ComputePoolRecord,
    machine_id: str | None,
) -> list[ComputeJoinCredentialRecord]:
    with services.context.database.session() as session:
        return [
            item
            for item in ComputeJoinCredentialRepository(session).list_for_pool(
                pool.workspace_id,
                pool.name,
            )
            if item.machine_id == machine_id
        ]


def test_compensated_launch_attempt_renews_join_authority_and_idempotency_key(
    isolated_services: ApiServices,
) -> None:
    """A compensated attempt must be able to relaunch under the same operation.

    Both the join token and the provider idempotency key derive from the attempt, so
    advancing it yields fresh authority and a genuinely new launch rather than a
    permanent dead end against revoked authority.
    """
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    pool = _pool_for_capacity(isolated_services, alpha, "renew-join-authority-pool")
    acquisition = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        desired_unit=2,
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=8_192),
    )

    requested = isolated_services.compute.acquire_capacity(acquisition)
    assert requested.status is CapacityAcquisitionStatus.Requested

    # The reconciler revokes an overdue launch intent's unused credential.
    [existing] = _capacity_credentials(isolated_services, pool, requested.target_machine_id)
    with isolated_services.context.database.session() as session:
        ComputeJoinCredentialRepository(session).save(existing.revoke(now=utc_now()))
    alpha.hide_remote_reconciliations = 1

    renewed = isolated_services.compute.acquire_capacity(acquisition)

    assert renewed.status is CapacityAcquisitionStatus.Requested
    assert renewed.target_machine_id == requested.target_machine_id
    assert alpha.registration_tokens[-1] != alpha.registration_tokens[-2]
    credentials = _capacity_credentials(isolated_services, pool, requested.target_machine_id)
    active = [item for item in credentials if item.status is ComputeCredentialStatus.Active]
    revoked = [item for item in credentials if item.status is not ComputeCredentialStatus.Active]
    assert len(active) == 1
    assert len(revoked) == 1
    assert active[0].token_hash != revoked[0].token_hash
    assert active[0].use_count == 0


def test_consumed_join_authority_is_never_reminted(
    isolated_services: ApiServices,
) -> None:
    """A used credential means a machine already enrolled; a second must not be minted."""
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    pool = _pool_for_capacity(isolated_services, alpha, "consumed-join-authority-pool")
    acquisition = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        desired_unit=2,
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=8_192),
    )

    requested = isolated_services.compute.acquire_capacity(acquisition)
    assert requested.status is CapacityAcquisitionStatus.Requested

    [existing] = _capacity_credentials(isolated_services, pool, requested.target_machine_id)
    with isolated_services.context.database.session() as session:
        ComputeJoinCredentialRepository(session).save(existing.model_copy(update={"use_count": 1}))
    alpha.hide_remote_reconciliations = 1

    rejected = isolated_services.compute.acquire_capacity(acquisition)

    assert rejected.status is CapacityAcquisitionStatus.TemporarilyUnavailable
    assert len(_capacity_credentials(isolated_services, pool, requested.target_machine_id)) == 1


def test_provider_failure_is_typed_and_never_carries_upstream_text(
    isolated_services: ApiServices,
) -> None:
    """Provider exception text must not reach durable state or the returned reason.

    Upstream errors routinely embed presigned URLs, tokens, and account identifiers;
    the durable record and the caller get a typed code plus a bounded safe message.
    """
    secret = "ASIAEXAMPLESECRET/presigned?X-Amz-Signature=deadbeefcafe"
    alpha = _DirectProvider(name="alpha", offers=[_offer("alpha", hourly_cost_micros=100_000)])
    pool = _pool_for_capacity(isolated_services, alpha, "typed-provider-failure-pool")
    # Arm the failure only after pool setup so it lands on the acquisition launch.
    alpha.fail_after_launches = len(alpha.launch_calls)
    alpha.launch_failure_detail = f"RunInstances denied for {secret}"
    acquisition = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        desired_unit=2,
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=8_192),
    )

    result = isolated_services.compute.acquire_capacity(acquisition)

    assert result.status is CapacityAcquisitionStatus.TemporarilyUnavailable
    assert result.failure_code is CapacityFailureCode.ProviderLaunchFailed
    assert secret not in result.reason
    assert "RunInstances denied" not in result.reason
    with isolated_services.context.database.session() as session:
        [operation] = ComputeCapacityOperationRepository(session).list_for_owner(
            pool.capacity_owner_id
        )
    assert operation.failure_code is CapacityFailureCode.ProviderLaunchFailed
    assert secret not in operation.last_error
    assert "RunInstances denied" not in operation.last_error
