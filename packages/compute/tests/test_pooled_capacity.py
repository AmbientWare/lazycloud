from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from compute.agent_control import agent_machine_worker_id
from compute.offers import ComputeOffer
from compute.policy import (
    AwsDefaultCapacityBaseline,
    ComputeCatalogInstance,
    ComputeCatalogRegion,
    WorkspaceComputePolicyService,
)
from compute.projection import PrivatePoolState
from compute.providers import (
    ComputeProviderResolver,
    ProviderCapacityPhase,
    ProviderPoolBootstrap,
    ProviderPoolInstance,
    ProviderPoolRequest,
    ProviderPoolSnapshot,
    ResolvedComputeProvider,
)
from compute.reclaim import ComputeReclaimPolicy
from compute.service import ComputeService
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeCapacityOperationRecord,
    ComputeCapacityOperationRepository,
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
    ComputePoolRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    TailnetCleanupTombstoneRepository,
    WorkspaceComputePolicyRepository,
)
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    PoolRepository,
    WorkerRepository,
)
from database.repositories.source_cache import SourceCacheCleanupRepository
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.capacity import (
    CapacityAcquisitionPlanningRequest,
    CapacityAcquisitionRequest,
    CapacityAcquisitionShape,
    CapacityAcquisitionStatus,
    CapacityPoolSizingStateUpdate,
    CapacityReleaseRequest,
)
from shared.compute_enrollment import (
    ComputeCredentialStatus,
    ComputeMachineEnrollmentStatus,
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
    MachineReadinessPhase,
    TailnetEnrollmentPhase,
)
from shared.compute_fleet import Machine, ResourceStatus, Worker
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputePlacementTarget,
    ComputePoolPhase,
    ComputePoolProviderState,
    ComputePoolRecord,
    ComputeResourceRequirements,
    WorkspaceComputePolicy,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.source_cache_cleanup import WorkerCacheGenerationState

_CONNECTION_ID = "11111111-1111-4111-8111-111111111111"


@dataclass(slots=True)
class _PooledProvider:
    desired: int = 0
    ensure_calls: list[ProviderPoolRequest] = field(default_factory=list)
    describe_calls: list[ProviderPoolRequest] = field(default_factory=list)
    capacity_calls: list[tuple[int, int]] = field(default_factory=list)
    delete_calls: list[ProviderPoolRequest] = field(default_factory=list)
    release_calls: list[str] = field(default_factory=list)
    lingering_storage: set[str] = field(default_factory=set)
    before_capacity: Callable[[ProviderPoolRequest], None] | None = None
    capacity_failure: Exception | None = None

    def list_offers(self) -> Iterable[ComputeOffer]:
        return (_offer(),)

    def ensure_pool(self, request: ProviderPoolRequest) -> ProviderPoolSnapshot:
        self.ensure_calls.append(request)
        self.desired = request.desired_machines
        return self._snapshot(request)

    def describe_pool(self, request: ProviderPoolRequest) -> ProviderPoolSnapshot:
        self.describe_calls.append(request)
        return self._snapshot(request)

    def set_pool_capacity(
        self,
        request: ProviderPoolRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderPoolSnapshot:
        self.capacity_calls.append((desired_machines, max_machines))
        if self.before_capacity is not None:
            self.before_capacity(request)
        if self.capacity_failure is not None:
            raise self.capacity_failure
        self.desired = desired_machines
        return self._snapshot(
            request.model_copy(
                update={
                    "desired_machines": desired_machines,
                    "max_machines": max_machines,
                }
            )
        )

    def release_machine(
        self,
        request: ProviderPoolRequest,
        provider_instance_id: str,
    ) -> ProviderPoolSnapshot:
        self.release_calls.append(provider_instance_id)
        self.desired = max(self.desired - 1, 0)
        return self._snapshot(request.model_copy(update={"desired_machines": self.desired}))

    def delete_pool(self, request: ProviderPoolRequest) -> ProviderPoolSnapshot:
        self.delete_calls.append(request)
        self.desired = 0
        return self._snapshot(request, phase=ProviderCapacityPhase.Deleted)

    def machine_storage_destroyed(
        self,
        request: ProviderPoolRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        del request
        del storage_volume_ids
        return provider_instance_id not in self.lingering_storage

    def _snapshot(
        self,
        request: ProviderPoolRequest,
        *,
        phase: ProviderCapacityPhase = ProviderCapacityPhase.Ready,
    ) -> ProviderPoolSnapshot:
        instances = [
            ProviderPoolInstance(
                provider_instance_id=f"i-{index:017x}",
                status="active",
                storage_volume_ids=(f"vol-{index:017x}",),
            )
            for index in range(self.desired)
        ]
        return ProviderPoolSnapshot(
            phase=phase,
            resource_id="asg-hidden",
            desired_machines=self.desired,
            max_machines=request.max_machines,
            observed_machines=len(instances),
            instances=instances,
            provider_state=ComputePoolProviderState(resource_id="asg-hidden"),
        )


@dataclass(slots=True)
class _AsyncScaleDownProvider(_PooledProvider):
    def set_pool_capacity(
        self,
        request: ProviderPoolRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderPoolSnapshot:
        if desired_machines != 0:
            return super().set_pool_capacity(
                request,
                desired_machines=desired_machines,
                max_machines=max_machines,
            )
        self.capacity_calls.append((desired_machines, max_machines))
        self.desired = 0
        instance = ProviderPoolInstance(
            provider_instance_id="i-00000000000000000",
            status="terminating",
            storage_volume_ids=("vol-00000000000000000",),
        )
        return ProviderPoolSnapshot(
            phase=ProviderCapacityPhase.Ready,
            resource_id="asg-hidden",
            desired_machines=0,
            max_machines=max_machines,
            observed_machines=1,
            instances=[instance],
            provider_state=ComputePoolProviderState(resource_id="asg-hidden"),
        )


@dataclass(slots=True)
class _MutationLeases:
    on_acquire: Callable[[str], None] | None = None
    acquired: list[str] = field(default_factory=list)
    held: set[str] = field(default_factory=set)

    @contextmanager
    def mutation_lock(self, capacity_owner_id: str) -> Iterator[None]:
        if capacity_owner_id in self.held:
            raise ConflictError(f"capacity owner lease already held: {capacity_owner_id}")
        self.held.add(capacity_owner_id)
        self.acquired.append(capacity_owner_id)
        try:
            if self.on_acquire is not None:
                self.on_acquire(capacity_owner_id)
            yield
        finally:
            self.held.remove(capacity_owner_id)


@dataclass(frozen=True, slots=True)
class _Resolver(ComputeProviderResolver):
    provider: _PooledProvider

    def list_providers(self, workspace_id: str) -> Iterable[ResolvedComputeProvider]:
        del workspace_id
        return (self._resolved(),)

    def resolve(self, workspace_id: str, provider_ref: str) -> ResolvedComputeProvider:
        del workspace_id
        if provider_ref != f"aws:{_CONNECTION_ID}":
            raise KeyError(provider_ref)
        return self._resolved()

    def _resolved(self) -> ResolvedComputeProvider:
        return ResolvedComputeProvider(
            ref=f"aws:{_CONNECTION_ID}",
            capacity_mode=ComputeCapacityMode.Pooled,
            connection_id=_CONNECTION_ID,
            pooled=self.provider,
        )


@dataclass(slots=True)
class _SchedulerHooks:
    retired: list[tuple[str, str, str, str]] = field(default_factory=list)
    revoked_join_tokens: list[str] = field(default_factory=list)

    def register_pool(self, state: PrivatePoolState) -> None:
        del state

    def register_machine(self, machine: Machine) -> None:
        del machine

    def register_internal_pool(self, pool: ComputePoolRecord, offer: ComputeOffer) -> None:
        del pool, offer

    def disable_machine(self, machine_id: str, reason: str) -> None:
        del machine_id, reason

    def retire_machine(
        self,
        workspace_id: str,
        pool_name: str,
        machine_id: str,
        reason: str,
    ) -> None:
        self.retired.append((workspace_id, pool_name, machine_id, reason))

    def revoke_pool_join_token(self, token_hash: str) -> None:
        self.revoked_join_tokens.append(token_hash)


@pytest.mark.parametrize(
    (
        "initial_desired",
        "workspace_limit",
        "requested_desired",
        "expected_desired",
        "expected_max",
        "expected_capacity_calls",
        "expect_capacity_conflict",
    ),
    [
        (0, 20, 15, 15, 16, [(15, 16)], False),
        (5, 3, 3, 3, 3, [(3, 3)], False),
        (0, 0, 1, 0, 0, [], True),
    ],
)
def test_internal_pool_scale_enforces_workspace_capacity_limit(
    isolated_services: ApiServices,
    initial_desired: int,
    workspace_limit: int,
    requested_desired: int,
    expected_desired: int,
    expected_max: int,
    expected_capacity_calls: list[tuple[int, int]],
    expect_capacity_conflict: bool,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(
            cpu_millicores=1_000,
            memory_mb=1_024,
        ),
        region="us-east-1",
        desired_machines=initial_desired,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()
    if workspace_limit == 20:
        with isolated_services.context.database.session() as session:
            sibling_pool_id = str(uuid4())
            ComputePoolRepository(session).upsert(
                pool.model_copy(
                    update={
                        "id": sibling_pool_id,
                        "capacity_owner_id": sibling_pool_id,
                        "name": "internal-aws-cpu-sibling",
                        "selector": "internal-aws-cpu-sibling",
                        "capability_key": f"{pool.capability_key}:sibling",
                        "desired_machines": 4,
                        "max_machines": 4,
                    }
                )
            )
    _set_cpu_limit(
        isolated_services,
        workspace_id=pool.workspace_id,
        limit=workspace_limit,
    )

    if expect_capacity_conflict:
        with pytest.raises(ConflictError, match="capacity limit"):
            compute.scale_internal_pool(
                pool.workspace_id,
                pool.name,
                requested_desired,
                before_mutation=_allow_scale,
            )
    else:
        scaled = compute.scale_internal_pool(
            pool.workspace_id,
            pool.name,
            requested_desired,
            before_mutation=_allow_scale,
        )
        assert scaled.desired_machines == expected_desired
        assert scaled.max_machines == expected_max

    assert provider.desired == expected_desired
    assert provider.capacity_calls == expected_capacity_calls


def test_aws_default_capacity_is_one_durable_floor_preserved_by_placement(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )

    baseline = compute.reconcile_aws_default_capacity(
        workspace="default",
        region="us-east-1",
        instance_type="i4i.xlarge",
        initial_machines=1,
        min_machines=1,
        max_machines=10,
        min_free_cpu_millicores=1_000,
        min_free_memory_mib=1_024,
        root_volume_gib=200,
        idle_timeout_seconds=300,
    )
    with isolated_services.context.database.session() as session:
        policy = PoolRepository(session).get(
            baseline.name,
            workspace_id=baseline.workspace_id,
        )
        assert policy is not None
        sibling_id = str(uuid4())
        ComputePoolRepository(session).upsert(
            baseline.model_copy(
                update={
                    "id": sibling_id,
                    "capacity_owner_id": sibling_id,
                    "name": "larger-demand-owned-cpu",
                    "selector": "larger-demand-owned-cpu",
                    "capability_key": f"{baseline.capability_key}:larger",
                    "desired_machines": 1,
                    "min_machines": 1,
                }
            )
        )
        PoolRepository(session).upsert(
            policy.model_copy(
                update={
                    "capacity_owner_id": sibling_id,
                    "name": "larger-demand-owned-cpu",
                    "initial_workers": 1,
                    "min_workers": 1,
                    "min_free_cpu_millicores": 2_000,
                    "min_free_memory_mib": 2_048,
                }
            ),
            workspace_id=baseline.workspace_id,
        )
    baseline = compute.reconcile_aws_default_capacity(
        workspace="default",
        region="us-east-1",
        instance_type="i4i.xlarge",
        initial_machines=1,
        min_machines=1,
        max_machines=10,
        min_free_cpu_millicores=1_000,
        min_free_memory_mib=1_024,
        root_volume_gib=200,
        idle_timeout_seconds=300,
    )
    placed = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(
            cpu_millicores=1_000,
            memory_mb=1_024,
        ),
        region="us-east-1",
        desired_machines=0,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )

    with isolated_services.context.database.session() as session:
        internal = ComputePoolRepository(session).list_internal(workspace_id=baseline.workspace_id)
        policies = {
            item.name: item
            for item in PoolRepository(session).list(workspace_id=baseline.workspace_id)
        }
    assert placed.id == baseline.id
    # Demand-driven placement never shrinks a pool it did not size.
    assert placed.desired_machines == 1
    assert [pool.id for pool in internal if pool.min_machines > 0] == [baseline.id]
    policy = policies[baseline.name]
    assert policy.initial_workers == 1
    assert policy.min_workers == 1
    assert policy.min_free_cpu_millicores == 1_000
    assert policy.min_free_memory_mib == 1_024
    larger_policy = policies["larger-demand-owned-cpu"]
    assert larger_policy.initial_workers == 0
    assert larger_policy.min_workers == 0
    assert larger_policy.min_free_cpu_millicores == 0
    assert larger_policy.min_free_memory_mib == 0

    compute.clear_aws_default_capacity(workspace="default", release_capacity=False)
    with isolated_services.context.database.session() as session:
        cleared = ComputePoolRepository(session).get(baseline.id)
        cleared_policy = PoolRepository(session).get(
            baseline.name,
            workspace_id=baseline.workspace_id,
        )
    assert cleared is not None
    assert cleared.min_machines == 0
    assert cleared_policy is not None
    assert cleared_policy.initial_workers == 0
    assert cleared_policy.min_workers == 0
    assert cleared_policy.min_free_cpu_millicores == 0
    assert cleared_policy.min_free_memory_mib == 0
    drained = compute.scale_internal_pool(
        baseline.workspace_id,
        baseline.name,
        0,
        before_mutation=_allow_scale,
    )
    assert drained.desired_machines == 0


def test_scale_zero_persists_intent_and_retires_sizing_before_provider_mutation(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    leases = _MutationLeases()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=leases,
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()
    started_at = datetime(2026, 7, 22, 12, tzinfo=UTC)
    sizing = compute.get_pool_sizing_state(pool.capacity_owner_id)
    compute.compare_and_set_pool_sizing_state(
        CapacityPoolSizingStateUpdate(
            capacity_owner_id=pool.capacity_owner_id,
            expected_revision=sizing.revision,
            operation_id="pending-scale-up",
            target_units=1,
            operation_started_at=started_at - timedelta(minutes=3),
            last_scale_up_at=started_at - timedelta(minutes=3),
            retry_after_at=started_at + timedelta(minutes=2),
            consecutive_failures=3,
            terminal_reason="capacity retry pending",
        )
    )
    before = compute.get_internal_pool(pool.workspace_id, pool.name)
    guard_observations: list[int] = []

    def guard(current: ComputePoolRecord) -> None:
        assert current.capacity_owner_id in leases.held
        guard_observations.append(current.desired_machines)

    def inspect_durable_intent(request: ProviderPoolRequest) -> None:
        with isolated_services.context.database.session() as session:
            durable = ComputePoolRepository(session).get(request.pool_id)
            retired = PoolRepository(session).get_sizing_state(request.pool_id)
        assert durable is not None
        assert durable.desired_machines == 0
        assert durable.generation == before.generation + 1
        assert durable.phase is ComputePoolPhase.Updating
        assert retired is not None
        assert retired.initial_target_reached
        assert retired.operation_id == ""
        assert retired.target_units == 0
        assert retired.operation_started_at is None
        assert retired.last_scale_up_at is not None
        assert retired.last_scale_up_at.replace(tzinfo=UTC) == started_at - timedelta(minutes=3)
        assert retired.last_scale_down_at is not None
        assert retired.last_scale_down_at.replace(tzinfo=UTC) == started_at
        assert retired.retry_after_at is None
        assert retired.consecutive_failures == 0
        assert retired.terminal_reason == ""

    provider.before_capacity = inspect_durable_intent
    scaled = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        0,
        before_mutation=guard,
        now=started_at,
    )

    assert guard_observations == [1]
    assert leases.acquired[-1] == pool.capacity_owner_id
    assert scaled.desired_machines == 0
    assert scaled.observed_machines == 0
    assert scaled.phase is ComputePoolPhase.Ready


def test_scale_zero_retains_degraded_intent_and_repairs_provider_failure(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()
    provider.capacity_failure = RuntimeError("provider request failed")

    with pytest.raises(UpstreamUnavailableError, match="provider capacity update failed"):
        compute.scale_internal_pool(
            pool.workspace_id,
            pool.name,
            0,
            before_mutation=_allow_scale,
        )

    degraded = compute.get_internal_pool(pool.workspace_id, pool.name)
    sizing = compute.get_pool_sizing_state(pool.capacity_owner_id)
    assert degraded.desired_machines == 0
    assert degraded.observed_machines == 1
    assert degraded.phase is ComputePoolPhase.Degraded
    assert sizing.initial_target_reached
    assert sizing.operation_id == ""
    assert sizing.target_units == 0
    provider.capacity_failure = None

    repaired = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        0,
        before_mutation=_allow_scale,
    )

    assert provider.capacity_calls == [(0, 10), (0, 10)]
    assert repaired.desired_machines == 0
    assert repaired.observed_machines == 0
    assert repaired.phase is ComputePoolPhase.Ready


def test_internal_pool_scale_maps_mutation_coordinator_failure(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()

    def fail_acquire(_capacity_owner_id: str) -> None:
        raise RuntimeError("redis unavailable")

    leases = _MutationLeases(on_acquire=fail_acquire)
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=leases,
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )

    with pytest.raises(UpstreamUnavailableError, match="mutation lease is unavailable"):
        compute.scale_internal_pool(
            pool.workspace_id,
            pool.name,
            0,
            before_mutation=_allow_scale,
        )


def test_scale_zero_skips_provider_only_after_durable_convergence(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=0,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()
    first = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        0,
        before_mutation=_allow_scale,
    )
    generation = first.generation

    second = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        0,
        before_mutation=_allow_scale,
    )

    assert provider.capacity_calls == [(0, 10)]
    assert len(provider.describe_calls) == 1
    assert provider.describe_calls[0].desired_machines == 0
    assert second.generation == generation
    assert second == first


def test_scale_zero_repairs_fresh_provider_drift_without_restoring_nonzero_intent(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=0,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()
    converged = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        0,
        before_mutation=_allow_scale,
    )
    provider.desired = 1

    def inspect_repair_intent(request: ProviderPoolRequest) -> None:
        with isolated_services.context.database.session() as session:
            durable = ComputePoolRepository(session).get(request.pool_id)
        assert durable is not None
        assert durable.desired_machines == 0
        assert durable.observed_machines == 1
        assert durable.phase is ComputePoolPhase.Updating

    provider.before_capacity = inspect_repair_intent
    repaired = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        0,
        before_mutation=_allow_scale,
    )

    assert len(provider.describe_calls) == 1
    assert provider.capacity_calls == [(0, 10), (0, 10)]
    assert repaired.generation == converged.generation + 1
    assert repaired.desired_machines == 0
    assert repaired.observed_machines == 0
    assert repaired.phase is ComputePoolPhase.Ready


def test_scale_zero_terminalizes_missing_provider_instance_projections(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()
    missing_since = datetime.now(UTC) - timedelta(minutes=5)
    with isolated_services.context.database.session() as session:
        repository = ComputeProviderInstanceRepository(session)
        [active] = repository.list_for_pool(pool.id)
        pending_with_instance = active.model_copy(
            update={
                "id": str(uuid4()),
                "status": "pending",
                "instance_id": "i-pending0000000001",
                "machine_id": None,
                "metadata": {
                    **active.metadata,
                    "missing_since": missing_since.isoformat(),
                    "storage_volume_ids": [],
                },
            }
        )
        planned_without_instance = active.model_copy(
            update={
                "id": str(uuid4()),
                "status": "pending",
                "instance_id": None,
                "machine_id": None,
                "metadata": {
                    **active.metadata,
                    "missing_since": missing_since.isoformat(),
                    "storage_volume_ids": [],
                },
            }
        )
        repository.upsert(pending_with_instance)
        repository.upsert(planned_without_instance)
        before = {item.id: item.created_at for item in repository.list_for_pool(pool.id)}
        orphaned_operation = ComputeCapacityOperationRepository(session).upsert(
            ComputeCapacityOperationRecord(
                id=str(uuid4()),
                workspace_id=pool.workspace_id,
                pool_id=pool.id,
                capacity_owner_id=pool.capacity_owner_id,
                reservation_id=str(uuid4()),
                operation_id=str(uuid4()),
                desired_unit=1,
                status="requested",
                owns_capacity=True,
            )
        )

    scaled = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        0,
        before_mutation=_allow_scale,
    )

    with isolated_services.context.database.session() as session:
        after = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        released_operation = ComputeCapacityOperationRepository(session).get(
            orphaned_operation.capacity_owner_id,
            orphaned_operation.operation_id,
        )
    assert scaled.desired_machines == 0
    assert scaled.observed_machines == 0
    assert scaled.phase is ComputePoolPhase.Ready
    assert {item.id: item.created_at for item in after} == before
    assert {item.status for item in after} == {"deleted"}
    assert all(item.metadata["terminated_reason"] == "provider_instance_missing" for item in after)
    assert all("provider_storage_destroyed_at" in item.metadata for item in after)
    assert released_operation is not None
    assert released_operation.status == "released"
    assert released_operation.release_desired_unit == 0


def test_reconcile_rereads_zero_intent_after_capacity_owner_lease(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    scale_compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = scale_compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    scale_compute.reconcile_pooled_capacity()
    provider.ensure_calls.clear()
    reconcile_leases = _MutationLeases()

    def scale_before_reconcile(capacity_owner_id: str) -> None:
        assert capacity_owner_id == pool.capacity_owner_id
        reconcile_leases.on_acquire = None
        scale_compute.scale_internal_pool(
            pool.workspace_id,
            pool.name,
            0,
            before_mutation=_allow_scale,
        )

    reconcile_leases.on_acquire = scale_before_reconcile
    reconciler = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=reconcile_leases,
    )

    reconciler.reconcile_pooled_capacity()

    assert provider.capacity_calls == [(0, 10)]
    assert [request.desired_machines for request in provider.ensure_calls] == [0]
    durable = reconciler.get_internal_pool(pool.workspace_id, pool.name)
    assert durable.desired_machines == 0
    assert durable.observed_machines == 0


def test_pooled_reconcile_fails_closed_without_capacity_owner_lease(
    isolated_services: ApiServices,
) -> None:
    compute = ComputeService(isolated_services.context)

    with pytest.raises(UpstreamUnavailableError, match="mutation lease"):
        compute.reconcile_pooled_capacity()


def test_pooled_capacity_plan_uses_authoritative_desired_state_without_mutation(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=0,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()
    provider.desired = 3
    capacity_calls_before_plan = list(provider.capacity_calls)
    ensure_calls_before_plan = list(provider.ensure_calls)

    planned = compute.plan_capacity_acquisition(
        CapacityAcquisitionPlanningRequest(
            capacity_owner_id=pool.capacity_owner_id,
            reservation_id=str(uuid4()),
            operation_id=str(uuid4()),
            shape=CapacityAcquisitionShape(
                cpu_millicores=4_000,
                memory_mib=32 * 1_024,
            ),
        )
    )

    assert planned.status is CapacityAcquisitionStatus.Requested
    assert planned.desired_unit == 4
    assert provider.capacity_calls == capacity_calls_before_plan
    assert provider.ensure_calls == ensure_calls_before_plan
    assert provider.desired == 3


def test_pooled_capacity_acquisition_is_idempotent_and_releases_only_its_unit(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=0,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()
    first = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        desired_unit=1,
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
    )
    second = CapacityAcquisitionRequest(
        capacity_owner_id=pool.capacity_owner_id,
        reservation_id=str(uuid4()),
        operation_id=str(uuid4()),
        desired_unit=2,
        shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32 * 1_024),
    )

    requested = compute.acquire_capacity(first)
    retried = compute.acquire_capacity(first)
    sibling = compute.acquire_capacity(second)
    released = compute.release_acquired_capacity(
        CapacityReleaseRequest(
            capacity_owner_id=first.capacity_owner_id,
            reservation_id=first.reservation_id,
            operation_id=first.operation_id,
        )
    )
    release_retry = compute.release_acquired_capacity(
        CapacityReleaseRequest(
            capacity_owner_id=first.capacity_owner_id,
            reservation_id=first.reservation_id,
            operation_id=first.operation_id,
        )
    )

    assert requested.status is CapacityAcquisitionStatus.Requested
    assert retried.status is CapacityAcquisitionStatus.ExistingPending
    assert sibling.status is CapacityAcquisitionStatus.Requested
    assert released.status is CapacityAcquisitionStatus.Requested
    assert release_retry.status is CapacityAcquisitionStatus.ExistingPending
    assert provider.capacity_calls == [(1, 10), (2, 10), (1, 10)]
    assert provider.desired == 1
    with isolated_services.context.database.session() as session:
        operations = ComputeCapacityOperationRepository(session).list_for_owner(
            pool.capacity_owner_id
        )
    assert [operation.status for operation in operations] == ["released", "requested"]


def test_connection_drain_deletes_hidden_capacity_idempotently(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(
            cpu_millicores=1_000,
            memory_mb=1_024,
        ),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()

    drained = compute.request_connection_drain(
        pool.provider_connection_id or "",
        workspace=pool.workspace_id,
    )
    repeated = compute.request_connection_drain(
        pool.provider_connection_id or "",
        workspace=pool.workspace_id,
    )
    reconciled = compute.reconcile_pooled_capacity()

    assert drained.total_pools == 1
    assert drained.remaining_pools == 0
    assert repeated == drained
    assert reconciled == []
    assert len(provider.ensure_calls) == 1
    assert len(provider.delete_calls) == 1


def test_pooled_scale_down_waits_for_exact_volume_absence(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    instance_id = "i-00000000000000000"
    provider = _PooledProvider(lingering_storage={instance_id})
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    started_at = datetime.now(UTC)
    compute.reconcile_pooled_capacity(now=started_at)
    machine_id = "33333333-3333-4333-8333-333333333333"
    generation_id = "44444444-4444-4444-8444-444444444444"
    with isolated_services.context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=machine_id,
                pool=pool.name,
                provider="agent",
                status=ResourceStatus.Running,
            ),
            workspace_id=pool.workspace_id,
        )
        bound = ComputeProviderInstanceRepository(session).bind_machine(
            pool.id,
            instance_id,
            machine_id,
        )
        assert bound is not None
        SourceCacheCleanupRepository(session).register_generation(
            generation_id,
            worker_id=f"worker-{machine_id}",
            storage_id=f"machine:{machine_id}",
            workspace_id=pool.workspace_id,
            now=started_at,
        )
    scaling = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        0,
        before_mutation=_allow_scale,
    )
    assert scaling.phase is ComputePoolPhase.Updating

    compute.reconcile_pooled_capacity(now=started_at + timedelta(seconds=121))
    with isolated_services.context.database.session() as session:
        [lingering] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        active_generation = SourceCacheCleanupRepository(session).get_generation(generation_id)
        updating_pool = ComputePoolRepository(session).get(pool.id)
    assert lingering.status != "deleted"
    assert lingering.metadata["storage_volume_ids"] == ["vol-00000000000000000"]
    assert active_generation is not None
    assert active_generation.state is not WorkerCacheGenerationState.Retired
    assert updating_pool is not None
    assert updating_pool.phase is ComputePoolPhase.Updating

    provider.lingering_storage.clear()
    compute.reconcile_pooled_capacity(now=started_at + timedelta(seconds=122))
    with isolated_services.context.database.session() as session:
        [destroyed] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        retired_generation = SourceCacheCleanupRepository(session).get_generation(generation_id)
        ready_pool = ComputePoolRepository(session).get(pool.id)
    assert destroyed.status == "deleted"
    assert "provider_storage_destroyed_at" in destroyed.metadata
    assert retired_generation is not None
    assert retired_generation.state is WorkerCacheGenerationState.Retired
    assert ready_pool is not None
    assert ready_pool.phase is ComputePoolPhase.Ready


def test_pooled_scale_down_projects_updating_during_provider_termination(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _AsyncScaleDownProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()

    scaling = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        0,
        before_mutation=_allow_scale,
    )

    assert scaling.desired_machines == 0
    assert scaling.observed_machines == 1
    assert scaling.phase is ComputePoolPhase.Updating


def test_connection_drain_terminalizes_provider_nodes_and_preserves_history(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    hooks = _SchedulerHooks()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
        scheduler_hooks=hooks,
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(
            cpu_millicores=1_000,
            memory_mb=1_024,
        ),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    compute.reconcile_pooled_capacity()
    first = compute.request_connection_drain(
        pool.provider_connection_id or "",
        workspace=pool.workspace_id,
    )
    machine_id = "33333333-3333-4333-8333-333333333333"
    worker_id = agent_machine_worker_id(machine_id)
    now = datetime.now(UTC)
    with isolated_services.context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=machine_id,
                pool=pool.name,
                provider="agent",
                status=ResourceStatus.Running,
            ),
            workspace_id=pool.workspace_id,
        )
        WorkerRepository(session).upsert(
            Worker(
                id=worker_id,
                machine_id=machine_id,
                pool=pool.name,
                status=ResourceStatus.Running,
            ),
            workspace_id=pool.workspace_id,
        )
        credential = ComputeJoinCredentialRepository(session).create(
            token_hash="a" * 64,
            workspace_id=pool.workspace_id,
            pool_name=pool.name,
            created_by_token_id=None,
            max_uses=1,
            expires_at=now + timedelta(minutes=2),
        )
        ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                workspace_id=pool.workspace_id,
                pool_name=pool.name,
                machine_id=machine_id,
                machine_fingerprint_hash="b" * 64,
                join_credential_id=credential.id,
                credential_hash="c" * 64,
                status=ComputeMachineEnrollmentStatus.Active,
                preflight_passed=True,
                heartbeat_confirmed=True,
                schedulable=True,
                readiness_phase=MachineReadinessPhase.Ready,
                tailnet_generation=1,
                tailnet_phase=TailnetEnrollmentPhase.Bound,
                tailnet_device_id="device-1",
                last_join_at=now,
                last_heartbeat_at=now,
            )
        )
        bound = ComputeProviderInstanceRepository(session).bind_machine(
            pool.id,
            "i-00000000000000000",
            machine_id,
        )
        assert bound is not None

    repeated = compute.request_connection_drain(
        pool.provider_connection_id or "",
        workspace=pool.workspace_id,
    )

    assert first.remaining_pools == 0
    assert repeated == first
    assert len(provider.delete_calls) == 1
    assert compute.list_machines(workspace=pool.workspace_id) == []
    with isolated_services.context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            pool.workspace_id,
            machine_id,
            pool_name=pool.name,
        )
        machine = MachineRepository(session).get(machine_id, workspace_id=pool.workspace_id)
        worker = WorkerRepository(session).get(worker_id, workspace_id=pool.workspace_id)
        durable_credential = ComputeJoinCredentialRepository(session).get(credential.id)
        tombstone = TailnetCleanupTombstoneRepository(session).get_by_machine(machine_id)
    assert enrollment is not None
    assert enrollment.status is ComputeMachineEnrollmentStatus.Deleted
    assert enrollment.schedulable is False
    assert enrollment.heartbeat_confirmed is False
    assert enrollment.readiness_phase is MachineReadinessPhase.Revoked
    assert machine is not None and machine.status is ResourceStatus.Deleted
    assert worker is not None and worker.status is ResourceStatus.Deleted
    assert durable_credential is not None
    assert durable_credential.status is ComputeCredentialStatus.Revoked
    assert tombstone is not None
    assert tombstone.generations == [1]
    assert tombstone.device_ids == ["device-1"]
    assert {item[2] for item in hooks.retired} == {machine_id}
    assert set(hooks.revoked_join_tokens) == {credential.token_hash}


def test_internal_pool_bootstrap_phase_deadline_reclaims_only_after_it_elapses(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    started_at = datetime.now(UTC)
    compute.reconcile_pooled_capacity(now=started_at)
    booting = _mark_open_record_booting(isolated_services, pool.id, at=started_at)
    assert booting.machine_id is None

    compute.reconcile_pooled_capacity(now=started_at + timedelta(seconds=200))
    with isolated_services.context.database.session() as session:
        [waiting] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert provider.release_calls == []
    assert waiting.status not in {"terminating", "deleted", "failed"}
    assert waiting.bootstrap_phase is MachineBootstrapPhase.Booting
    assert waiting.bootstrap_observed_at == started_at

    compute.reconcile_pooled_capacity(now=started_at + timedelta(seconds=301))
    with isolated_services.context.database.session() as session:
        [replacement] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        current = ComputePoolRepository(session).get(pool.id)
    assert provider.release_calls == [booting.instance_id]
    assert replacement.launch_attempt == 2
    assert replacement.bootstrap_phase is MachineBootstrapPhase.Provisioning
    assert replacement.bootstrap_failure_reason is None
    assert replacement.machine_id is None
    assert "terminating_reason" not in replacement.metadata
    assert current is not None
    assert current.provider_state.degraded_reason is None
    assert current.desired_machines == 1
    assert provider.desired == 1


def test_relaunch_exhaustion_durably_degrades_pool_until_explicit_capacity_mutation(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
        reclaim=ComputeReclaimPolicy(max_launch_attempts=2),
    )
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    moment = datetime.now(UTC)
    compute.reconcile_pooled_capacity(now=moment)
    first = _mark_open_record_booting(isolated_services, pool.id, at=moment)
    assert first.launch_attempt == 1

    moment += timedelta(seconds=301)
    compute.reconcile_pooled_capacity(now=moment)
    relaunched = _mark_open_record_booting(isolated_services, pool.id, at=moment)
    assert relaunched.launch_attempt == 2

    moment += timedelta(seconds=301)
    ensure_calls_before_exhaustion = len(provider.ensure_calls)
    compute.reconcile_pooled_capacity(now=moment)
    with isolated_services.context.database.session() as session:
        degraded = ComputePoolRepository(session).get(pool.id)
        [reclaimed] = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
    assert degraded is not None
    assert degraded.phase is ComputePoolPhase.Degraded
    assert degraded.provider_state.degraded_reason == "bootstrap_launch_attempts_exhausted"
    assert reclaimed.status == "deleted"
    assert reclaimed.bootstrap_phase is MachineBootstrapPhase.Failed
    assert reclaimed.bootstrap_failure_reason is MachineBootstrapFailureReason.BootstrapTimedOut
    assert reclaimed.metadata["terminating_reason"] == "bootstrap_deadline_exceeded"
    assert reclaimed.metadata["provider_storage_destroyed_at"]
    assert provider.desired == 0
    assert len(provider.ensure_calls) == ensure_calls_before_exhaustion

    compute.reconcile_pooled_capacity(now=moment + timedelta(seconds=301))
    with isolated_services.context.database.session() as session:
        still_degraded = ComputePoolRepository(session).get(pool.id)
    assert still_degraded is not None
    assert still_degraded.provider_state.degraded_reason == "bootstrap_launch_attempts_exhausted"
    assert provider.desired == 0
    assert len(provider.ensure_calls) == ensure_calls_before_exhaustion

    scaled = compute.scale_internal_pool(
        pool.workspace_id,
        pool.name,
        1,
        before_mutation=_allow_scale,
    )
    assert scaled.provider_state.degraded_reason is None
    assert scaled.desired_machines == 1
    assert provider.desired == 1


def test_zero_capacity_policy_update_drives_internal_pool_desired_to_zero(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    baseline = compute.reconcile_aws_default_capacity(
        workspace="default",
        region="us-east-1",
        instance_type="i4i.xlarge",
        initial_machines=1,
        min_machines=1,
        max_machines=10,
        min_free_cpu_millicores=1_000,
        min_free_memory_mib=1_024,
        root_volume_gib=200,
        idle_timeout_seconds=300,
    )
    compute.reconcile_pooled_capacity()
    assert baseline.desired_machines == 1
    assert provider.desired == 1

    policies = WorkspaceComputePolicyService(
        isolated_services.context,
        available_catalog=(
            ComputeCatalogRegion(
                region="us-east-1",
                instances=(
                    ComputeCatalogInstance(
                        instance_type="i4i.xlarge",
                        kind="cpu",
                        cpu_millicores=4_000,
                        memory_mb=32 * 1024,
                    ),
                ),
            ),
        ),
        aws_default_capacity=AwsDefaultCapacityBaseline(capacity=compute),
    )
    current = policies.get_policy(workspace="default")
    policies.update_policy(
        workspace="default",
        expected_revision=current.revision,
        # AWS stays the default placement: zeroing the policy is the only control
        # the user is given, and it has to release the machine on its own.
        default_placement=ComputePlacementTarget.Aws,
        aws=current.aws.model_copy(
            update={
                "initial_cpu_workers": 0,
                "min_cpu_workers": 0,
                "max_cpu_instances": 0,
                "max_gpu_instances": 0,
            }
        ),
    )

    with isolated_services.context.database.session() as session:
        drained = ComputePoolRepository(session).get(baseline.id)
    assert drained is not None
    assert drained.min_machines == 0
    assert drained.desired_machines == 0
    assert provider.desired == 0
    # Proves the guarded scale owner ran rather than a bare zero record being
    # written, which leaves the sizing state to raise the machine straight back.
    assert (0, 1) in provider.capacity_calls


def test_policy_owned_capacity_tracks_lowered_and_raised_bounds(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )

    def reconcile(*, floor: int, ceiling: int) -> ComputePoolRecord:
        return compute.reconcile_aws_default_capacity(
            workspace="default",
            region="us-east-1",
            instance_type="i4i.xlarge",
            initial_machines=floor,
            min_machines=floor,
            max_machines=ceiling,
            min_free_cpu_millicores=1_000,
            min_free_memory_mib=1_024,
            root_volume_gib=200,
            idle_timeout_seconds=300,
        )

    assert reconcile(floor=1, ceiling=10).desired_machines == 1
    grown = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=5,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    assert grown.desired_machines == 5

    # Lowering the floor releases exactly the capacity that floor was holding and
    # leaves demand-grown capacity to the scheduler's own owners.
    assert reconcile(floor=0, ceiling=10).desired_machines == 4
    # Lowering the ceiling clamps desired down to it.
    assert reconcile(floor=0, ceiling=2).desired_machines == 2
    # Raising the floor still reconciles capacity upward.
    assert reconcile(floor=3, ceiling=10).desired_machines == 3


def test_lowered_policy_floor_does_not_terminate_a_machine_running_work(
    isolated_services: ApiServices,
) -> None:
    _seed_connection(isolated_services)
    provider = _PooledProvider()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
    )
    pool = compute.reconcile_aws_default_capacity(
        workspace="default",
        region="us-east-1",
        instance_type="i4i.xlarge",
        initial_machines=1,
        min_machines=1,
        max_machines=10,
        min_free_cpu_millicores=1_000,
        min_free_memory_mib=1_024,
        root_volume_gib=200,
        idle_timeout_seconds=300,
    )
    compute.reconcile_pooled_capacity()
    assert provider.desired == 1

    machine_id = "44444444-4444-4444-8444-444444444444"
    with isolated_services.context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=machine_id,
                pool=pool.name,
                provider=pool.provider_ref,
                status=ResourceStatus.Running,
            ),
            workspace_id=pool.workspace_id,
        )
        bound = ComputeProviderInstanceRepository(session).bind_machine(
            pool.id,
            "i-00000000000000000",
            machine_id,
        )
        assert bound is not None
        ContainerRepository(session).upsert(
            ContainerRecord(
                id="55555555-5555-4555-8555-555555555555",
                name="container-running",
                image="",
                command=[],
                workspace_id=pool.workspace_id,
                machine_id=machine_id,
                status=ContainerStatus.Running,
            )
        )

    policies = WorkspaceComputePolicyService(
        isolated_services.context,
        available_catalog=(
            ComputeCatalogRegion(
                region="us-east-1",
                instances=(
                    ComputeCatalogInstance(
                        instance_type="i4i.xlarge",
                        kind="cpu",
                        cpu_millicores=4_000,
                        memory_mb=32 * 1024,
                    ),
                ),
            ),
        ),
        aws_default_capacity=AwsDefaultCapacityBaseline(capacity=compute),
    )
    current = policies.get_policy(workspace="default")
    policies.update_policy(
        workspace="default",
        expected_revision=current.revision,
        default_placement=ComputePlacementTarget.Aws,
        aws=current.aws.model_copy(
            update={
                "initial_cpu_workers": 0,
                "min_cpu_workers": 0,
                "max_cpu_instances": 0,
                "max_gpu_instances": 0,
            }
        ),
    )

    with isolated_services.context.database.session() as session:
        held = ComputePoolRepository(session).get(pool.id)
    assert held is not None
    # The provider scales in by picking its own victim, so the running workload is
    # only safe while the floor still holds its machine.
    assert held.desired_machines == 1
    assert provider.desired == 1
    # The floor is gone, which is what lets the drain owner release the machine
    # once the work finishes.
    assert held.min_machines == 0


def _mark_open_record_booting(
    isolated_services: ApiServices,
    pool_id: str,
    *,
    at: datetime,
) -> ComputeProviderInstanceRecord:
    with isolated_services.context.database.session() as session:
        repository = ComputeProviderInstanceRepository(session)
        record = next(
            item
            for item in repository.list_for_pool(pool_id)
            if item.status not in {"deleted", "failed"}
        )
        return repository.upsert(
            record.model_copy(
                update={
                    "bootstrap_phase": MachineBootstrapPhase.Booting,
                    "bootstrap_observed_at": at,
                }
            )
        )


def _offer() -> ComputeOffer:
    return ComputeOffer(
        id="us-east-1:i4i.xlarge",
        provider="aws:11111111-1111-4111-8111-111111111111",
        cloud="aws",
        instance_type="i4i.xlarge",
        region="us-east-1",
        cpu_millicores=4_000,
        memory_mb=32 * 1024,
        storage_mb=200 * 1024,
        hourly_cost_micros=340_000,
        available=10,
        capacity_mode=ComputeCapacityMode.Pooled,
        capability_key="aws:us-east-1:i4i.xlarge:amd64:runc",
        supports_scale_to_zero=True,
    )


def _bootstrap(pool: ComputePoolRecord, offer: ComputeOffer) -> ProviderPoolBootstrap:
    del offer
    return ProviderPoolBootstrap(
        control_plane_url="https://control.example.com",
        enrollment_request_id=pool.id,
        agent_version="0.1.0",
        agent_sha256="a" * 64,
        agent_binary_url=(
            f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'a' * 64}/"
            "lazycloud-agent-linux-amd64"
        ),
        worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
    )


def _allow_scale(pool: ComputePoolRecord) -> None:
    del pool


def _set_cpu_limit(
    isolated_services: ApiServices,
    *,
    workspace_id: str,
    limit: int,
) -> None:
    now = datetime.now(UTC)
    with isolated_services.context.database.session() as session:
        policies = WorkspaceComputePolicyRepository(session)
        policy = policies.ensure_default(
            WorkspaceComputePolicy(
                id=str(uuid4()),
                workspace_id=workspace_id,
                created_at=now,
                updated_at=now,
            )
        )
        policies.save(
            policy.model_copy(
                update={
                    "revision": policy.revision + 1,
                    "aws": policy.aws.model_copy(
                        update={
                            "initial_cpu_workers": min(policy.aws.initial_cpu_workers, limit),
                            "min_cpu_workers": min(policy.aws.min_cpu_workers, limit),
                            "max_cpu_instances": limit,
                        }
                    ),
                    "updated_at": now,
                }
            )
        )


def _seed_connection(isolated_services: ApiServices) -> None:
    now = datetime.now(UTC)
    account_id = "123456789012"
    authorization = AwsAccountAuthorizationGeneration(
        id=str(uuid4()),
        generation=1,
        role_arn=f"arn:aws:iam::{account_id}:role/compute-control",
        authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
        phase=AwsAccountAuthorizationPhase.Ready,
        last_validated_at=now,
        created_at=now,
        updated_at=now,
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=_CONNECTION_ID,
                workspace_id=workspace_id,
                account_id=account_id,
                external_id="x" * 48,
                phase=AwsAccountConnectionPhase.Ready,
                active_authorization=authorization,
                node_role_arn=f"arn:aws:iam::{account_id}:role/compute-node",
                node_instance_profile_arn=(
                    f"arn:aws:iam::{account_id}:instance-profile/compute-node"
                ),
                created_at=now,
                updated_at=now,
            )
        )
