from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from compute.aws_configuration import AWS_COMPUTE_CONFIGURATION
from compute.block_volumes import BlockVolumeProvider
from compute.offers import ComputeOffer
from compute.providers import (
    ComputeProviderResolver,
    ComputeSchedulerHooks,
    MachineWorkerAvailability,
    ProviderCapacityPhase,
    ProviderOfferEligibility,
    ProviderUnitBootstrap,
    ProviderUnitInstance,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
    ResolvedComputeProvider,
    ResolvedProviderPolicy,
)
from compute.reclaim import ComputeReclaimPolicy
from compute.reserve_state import RedisFleetReserveState
from compute.service import ComputeServices
from database.context import ServiceContext
from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.compute import (
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
)
from database.repositories.orchestration import MachineRepository, WorkerRepository
from database.tables.orchestration import MachineTable
from database.types import DatabaseSession
from identity.platform import PlatformNamespaceService
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
    AwsAccountNetwork,
)
from shared.capacity import CapacityFailureCode
from shared.capacity_lifecycle import CapacitySleepRequest
from shared.compute_enrollment import (
    ComputeMachineEnrollmentStatus,
    MachineReadinessPhase,
    agent_machine_worker_id,
)
from shared.compute_fleet import Machine, MachineLifecycle, ResourceStatus, Worker
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeResourceRequirements,
    ComputeUnitProviderState,
    ComputeUnitRecord,
)
from shared.errors import ConflictError
from shared.network_egress import NetworkEgressRouteEvidence
from shared.placement import Placement
from shared.supplier_costs import SupplierCostTerms
from tests.workspaces import workspace_owner_user_id

_CONNECTION_ID = "11111111-1111-4111-8111-111111111111"


@dataclass(slots=True)
class _PooledProvider:
    reserve_offers: tuple[ComputeOffer, ...] = ()

    def list_reserve_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        return self.reserve_offers

    def complete_machine_preparation(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        *,
        sleep_request: CapacitySleepRequest | None,
    ) -> ProviderUnitSnapshot:
        raise AssertionError("test provider has no stopped capacity")

    def refresh_machine(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        raise AssertionError("test provider has no stopped capacity")

    def stop_machine(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        *,
        sleep_request: CapacitySleepRequest,
    ) -> ProviderUnitSnapshot:
        raise AssertionError("test provider has no stopped capacity")

    def unbilled_network_destinations(
        self, unit: ComputeUnitRecord, provider_instance_id: str
    ) -> NetworkEgressRouteEvidence:
        raise AssertionError("capacity lifecycle must not request network billing evidence")

    def block_volumes(self, region: str) -> BlockVolumeProvider:
        raise AssertionError("capacity lifecycle must not manage disk volumes")

    desired: int = 0
    offer: ComputeOffer = field(default_factory=lambda: _offer())
    ensure_calls: list[ProviderUnitRequest] = field(default_factory=list)
    describe_calls: list[ProviderUnitRequest] = field(default_factory=list)
    capacity_calls: list[tuple[int, int]] = field(default_factory=list)
    delete_calls: list[ProviderUnitRequest] = field(default_factory=list)
    release_calls: list[str] = field(default_factory=list)
    lingering_storage: set[str] = field(default_factory=set)
    before_capacity: Callable[[ProviderUnitRequest], None] | None = None
    capacity_failure: Exception | None = None
    delete_failure: Exception | None = None
    catalog_failure: Exception | None = None
    storage_failure: Exception | None = None
    last_capacity_failure_at: datetime | None = None
    last_capacity_failure_code: CapacityFailureCode = CapacityFailureCode.ProviderLaunchFailed
    max_observed_machines: int | None = None

    def unit_offer(self, unit: ComputeUnitRecord) -> ComputeOffer:
        return self.offer

    def list_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        if self.catalog_failure is not None:
            raise self.catalog_failure
        return (self.offer,)

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        self.ensure_calls.append(request)
        if request.purchases_enabled:
            self.desired = request.desired_machines
        return self._snapshot(request)

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        self.describe_calls.append(request)
        return self._snapshot(request)

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot:
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
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderUnitSnapshot:
        self.release_calls.append(provider_instance_id)
        self.desired = max(self.desired - 1, 0)
        return self._snapshot(request.model_copy(update={"desired_machines": self.desired}))

    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        if self.delete_failure is not None:
            raise self.delete_failure
        self.delete_calls.append(request)
        self.desired = 0
        return self._snapshot(request, phase=ProviderCapacityPhase.Deleted)

    def machine_storage_destroyed(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        del request
        del storage_volume_ids
        if self.storage_failure is not None:
            raise self.storage_failure
        return provider_instance_id not in self.lingering_storage

    def _snapshot(
        self,
        request: ProviderUnitRequest,
        *,
        phase: ProviderCapacityPhase = ProviderCapacityPhase.Ready,
    ) -> ProviderUnitSnapshot:
        instances = [
            ProviderUnitInstance(
                provider_instance_id=f"i-{index:017x}",
                status="active",
                storage_volume_ids=(f"vol-{index:017x}",),
            )
            for index in range(self.desired)
        ]
        if self.max_observed_machines is not None:
            instances = instances[: self.max_observed_machines]
        return ProviderUnitSnapshot(
            phase=phase,
            resource_id="asg-hidden",
            desired_machines=self.desired,
            stopped_machines=request.stopped_machines,
            max_machines=request.max_machines,
            observed_machines=len(instances),
            instances=instances,
            last_capacity_failure_at=self.last_capacity_failure_at,
            last_capacity_failure_code=self.last_capacity_failure_code,
            provider_state=ComputeUnitProviderState(resource_id="asg-hidden"),
        )


@dataclass(slots=True)
class _EmptyAccountProvider(_PooledProvider):
    """An account with no autoscaling group in it, however it came to be empty."""

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        self.describe_calls.append(request)
        return self._snapshot(request, phase=ProviderCapacityPhase.Deleted)


@dataclass(slots=True)
class _AsyncScaleDownProvider(_PooledProvider):
    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self.set_unit_capacity(
            request, desired_machines=0, max_machines=request.max_machines
        ).model_copy(update={"phase": ProviderCapacityPhase.Deleting})

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot:
        if desired_machines != 0:
            return super().set_unit_capacity(
                request,
                desired_machines=desired_machines,
                max_machines=max_machines,
            )
        self.capacity_calls.append((desired_machines, max_machines))
        self.desired = 0
        instance = ProviderUnitInstance(
            provider_instance_id="i-00000000000000000",
            status="terminating",
            storage_volume_ids=(),
        )
        return ProviderUnitSnapshot(
            phase=ProviderCapacityPhase.Ready,
            resource_id="asg-hidden",
            desired_machines=0,
            max_machines=max_machines,
            observed_machines=1,
            instances=[instance],
            provider_state=ComputeUnitProviderState(resource_id="asg-hidden"),
        )


@dataclass(slots=True)
class _MutationLeases:
    on_acquire: Callable[[str], None] | None = None
    acquired: list[str] = field(default_factory=list)
    held: set[str] = field(default_factory=set)
    dispatch_acquired: list[str] = field(default_factory=list)
    dispatch_held: set[str] = field(default_factory=set)
    reserved_owners: set[str] = field(default_factory=set)

    def has_open_reservations(self, capacity_owner_id: str) -> bool:
        assert capacity_owner_id in self.held and capacity_owner_id in self.dispatch_held
        return capacity_owner_id in self.reserved_owners

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

    @contextmanager
    def dispatch_lock(self, capacity_owner_id: str) -> Iterator[None]:
        if capacity_owner_id in self.dispatch_held:
            raise ConflictError(f"capacity owner dispatch lease already held: {capacity_owner_id}")
        self.dispatch_held.add(capacity_owner_id)
        self.dispatch_acquired.append(capacity_owner_id)
        try:
            yield
        finally:
            self.dispatch_held.remove(capacity_owner_id)


@dataclass(slots=True)
class _Resolver(ComputeProviderResolver):
    provider: _PooledProvider
    context: ServiceContext
    purchases_enabled: bool = True
    allowed_offers: tuple[ProviderOfferEligibility, ...] | None = None
    root_volume_gib: int = 200

    def list_platform_providers(self) -> Iterable[ResolvedComputeProvider]:
        return ()

    def list_providers(self, workspace_id: str) -> Iterable[ResolvedComputeProvider]:
        del workspace_id
        return (self._resolved(),)

    def resolve(self, workspace_id: str, provider_ref: str) -> ResolvedComputeProvider:
        del workspace_id
        if provider_ref != f"aws:{_CONNECTION_ID}":
            raise KeyError(provider_ref)
        return self._resolved()

    def _resolved(self) -> ResolvedComputeProvider:
        with self.context.database.session() as session:
            connection = AwsAccountConnectionRepository(session).get(_CONNECTION_ID)
            workspace_id = self.context.workspace(session, "default").id
        if connection is None:
            workspace_id = PlatformNamespaceService(self.context.database).get().id
        return ResolvedComputeProvider(
            ref=f"aws:{_CONNECTION_ID}",
            capacity_mode=ComputeCapacityMode.Pooled,
            connection_id=_CONNECTION_ID if connection is not None else None,
            pooled=self.provider,
            policy=ResolvedProviderPolicy(
                purchases_enabled=self.purchases_enabled,
                workspace_id=workspace_id,
                placement=connection.placement if connection is not None else Placement.platform(),
                platform_fleet=connection is None,
                root_volume_gib=self.root_volume_gib,
                default_region=AWS_COMPUTE_CONFIGURATION.default_region,
                allowed_regions=AWS_COMPUTE_CONFIGURATION.allowed_regions,
                allowed_offers=self.allowed_offers
                if self.allowed_offers is not None
                else (
                    ProviderOfferEligibility(
                        region=self.provider.offer.region,
                        instance_type=self.provider.offer.instance_type,
                    ),
                ),
            ),
        )


@dataclass(slots=True)
class _SchedulerHooks:
    retired: list[tuple[str, str, str]] = field(default_factory=list)
    available_machines: set[str] = field(default_factory=set)
    unknown_machines: set[str] = field(default_factory=set)
    revoked_join_tokens: list[str] = field(default_factory=list)
    intake_observing_since: datetime | None = None

    def register_internal_unit(self, unit: ComputeUnitRecord, offer: ComputeOffer) -> None:
        del unit, offer

    def disable_machine(self, machine_id: str, reason: str) -> None:
        del machine_id, reason

    def machine_worker_availability(self, machine_id: str) -> MachineWorkerAvailability:
        if machine_id in self.available_machines:
            return MachineWorkerAvailability.Available
        if machine_id in self.unknown_machines:
            return MachineWorkerAvailability.Unknown
        return MachineWorkerAvailability.Unavailable

    def agent_intake_observing_since(self) -> datetime | None:
        return self.intake_observing_since

    def retire_machine(
        self,
        workspace_id: str,
        machine_id: str,
        reason: str,
    ) -> None:
        self.retired.append((workspace_id, machine_id, reason))

    def revoke_unit_join_token(self, token_hash: str) -> None:
        self.revoked_join_tokens.append(token_hash)


def _bind_test_machine(
    session: DatabaseSession,
    pool_id: str,
    instance_id: str,
    machine_id: str,
) -> ComputeProviderInstanceRecord | None:
    repository = ComputeProviderInstanceRepository(session)
    record = repository.get_for_pool_instance(pool_id, instance_id, for_update=True)
    if record is not None and record.machine_id not in {None, machine_id}:
        auto_machine = session.get(MachineTable, record.machine_id)
        repository.upsert(record.model_copy(update={"machine_id": None}))
        if auto_machine is not None:
            session.delete(auto_machine)
        session.flush()
    return repository.bind_machine(pool_id, instance_id, machine_id)


def _machine_of(service_context: ServiceContext, record: ComputeProviderInstanceRecord) -> Machine:
    assert record.machine_id is not None
    with service_context.database.session() as session:
        machine = MachineRepository(session).get_across_workspaces(record.machine_id)
    assert machine is not None
    return machine


def _mark_open_record_booting(
    service_context: ServiceContext,
    pool_id: str,
    *,
    at: datetime,
) -> ComputeProviderInstanceRecord:
    with service_context.database.session() as session:
        repository = ComputeProviderInstanceRepository(session)
        record = next(
            item
            for item in repository.list_for_pool(pool_id)
            if item.status not in {"deleted", "failed"}
        )
        assert record.machine_id is not None
        machines = MachineRepository(session)
        machine = machines.get_across_workspaces(record.machine_id)
        assert machine is not None
        machines.upsert(
            machine.model_copy(
                update={
                    "lifecycle": MachineLifecycle.Booting,
                    "lifecycle_message": "",
                    "lifecycle_at": at,
                    "updated_at": at,
                }
            )
        )
        return record


def _offer() -> ComputeOffer:
    return ComputeOffer(
        id="us-east-1:m7i.xlarge",
        provider="aws:11111111-1111-4111-8111-111111111111",
        cloud="aws",
        instance_type="m7i.xlarge",
        region="us-east-1",
        cpu_millicores=4_000,
        memory_mb=32 * 1024,
        storage_mb=200 * 1024,
        cost_terms=SupplierCostTerms(
            compute_hourly_micros=340_000, root_disk_hourly_micros=0, public_ipv4_hourly_micros=0
        ),
        available=10,
        capacity_mode=ComputeCapacityMode.Pooled,
        capability_key="aws:us-east-1:m7i.xlarge:amd64:runsc",
        supports_scale_to_zero=True,
    )


class _Bootstrap:
    """A pool bootstrap provisioner with no external network dependency."""

    def __init__(self) -> None:
        self.released: list[str] = []

    def bootstrap(
        self,
        pool: ComputeUnitRecord,
        offer: ComputeOffer,
    ) -> ProviderUnitBootstrap:
        del offer
        return ProviderUnitBootstrap(
            control_plane_url="https://control.example.com",
            enrollment_request_id=pool.id,
            agent_version="0.1.0",
            agent_sha256="a" * 64,
            agent_binary_url=(
                f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'a' * 64}/"
                "lazycloud-agent-linux-amd64.tar.gz"
            ),
        )

    def release(self, pool: ComputeUnitRecord) -> None:
        self.released.append(pool.id)


_bootstrap = _Bootstrap()


def _allow_scale(pool: ComputeUnitRecord) -> None:
    del pool


def _seed_connection(service_context: ServiceContext, *, platform_fleet: bool = False) -> None:
    if platform_fleet:
        PlatformNamespaceService(service_context.database).initialize()
        return
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
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
    owner_id = workspace_owner_user_id(service_context, workspace_id)
    with service_context.database.session() as session:
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=_CONNECTION_ID,
                user_id=owner_id,
                account_id=account_id,
                external_id="x" * 48,
                phase=AwsAccountConnectionPhase.Ready,
                active_authorization=authorization,
                node_role_arn=f"arn:aws:iam::{account_id}:role/compute-node",
                node_instance_profile_arn=(
                    f"arn:aws:iam::{account_id}:instance-profile/compute-node"
                ),
                networks={
                    "us-east-1": AwsAccountNetwork(
                        vpc_id="vpc-01234567",
                        subnet_ids=("subnet-01234567", "subnet-89abcdef"),
                        security_group_id="sg-01234567",
                    )
                },
                created_at=now,
                updated_at=now,
            )
        )


def _seed_serving_machine(
    service_context: ServiceContext,
    pool: ComputeUnitRecord,
    hooks: _SchedulerHooks,
    *,
    machine_id: str,
    instance_id: str,
    now: datetime,
) -> None:

    worker_id = agent_machine_worker_id(machine_id)
    owner_id = (
        None if pool.platform_fleet else workspace_owner_user_id(service_context, pool.workspace_id)
    )
    with service_context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=machine_id,
                placement=pool.placement,
                provider="agent",
                status=ResourceStatus.Running,
                lifecycle=MachineLifecycle.Joining,
            ),
            workspace_id=pool.workspace_id,
        )
        WorkerRepository(session).upsert(
            Worker(
                id=worker_id,
                machine_id=machine_id,
                placement=pool.placement,
                status=ResourceStatus.Running,
            ),
            workspace_id=pool.workspace_id,
        )
        credential = ComputeJoinCredentialRepository(session).create(
            token_hash=machine_id.replace("-", "")[:16].ljust(64, "a"),
            user_id=owner_id,
            workspace_id=pool.workspace_id,
            capacity_owner_id=pool.capacity_owner_id,
            placement=pool.placement,
            created_by_token_id=None,
            max_uses=1,
            expires_at=now + timedelta(minutes=2),
        )
        ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=owner_id,
                workspace_id=pool.workspace_id,
                capacity_owner_id=pool.capacity_owner_id,
                placement=pool.placement,
                machine_id=machine_id,
                machine_fingerprint_hash=hashlib.sha256(machine_id.encode()).hexdigest(),
                join_credential_id=credential.id,
                credential_hash=hashlib.sha256(f"credential:{machine_id}".encode()).hexdigest(),
                status=ComputeMachineEnrollmentStatus.Active,
                preflight_passed=True,
                heartbeat_confirmed=True,
                schedulable=True,
                readiness_phase=MachineReadinessPhase.Ready,
                last_join_at=now,
                last_heartbeat_at=now,
            )
        )
        _bind_test_machine(session, pool.id, instance_id, machine_id)
    # The lifecycle write opens its own session, so the seed above must have
    # committed first: a second writer inside an uncommitted one deadlocks.
    compute = ComputeServices.create(service_context, capacity_owner_mutations=_MutationLeases())
    for lifecycle in (MachineLifecycle.Joining, MachineLifecycle.Ready):
        compute.machines.record_provider_node_lifecycle(
            pool_id=pool.id,
            provider_instance_id=instance_id,
            lifecycle=lifecycle,
            failure_reason=None,
            now=now,
        )
    hooks.available_machines.add(machine_id)


def pooled_service(
    service_context: ServiceContext,
    provider: _PooledProvider,
    *,
    scheduler_hooks: ComputeSchedulerHooks | None = None,
    reclaim: ComputeReclaimPolicy | None = None,
    reserve_state: RedisFleetReserveState | None = None,
) -> ComputeServices:
    return ComputeServices.create(
        service_context,
        provider_resolver=_Resolver(provider, service_context),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=_MutationLeases(),
        scheduler_hooks=scheduler_hooks,
        reclaim=reclaim,
        reserve_state=reserve_state,
    )


def _open_record(
    service_context: ServiceContext,
    pool_id: str,
) -> ComputeProviderInstanceRecord | None:
    with service_context.database.session() as session:
        return next(
            (
                item
                for item in ComputeProviderInstanceRepository(session).list_for_pool(pool_id)
                if item.status not in {"deleted", "failed", "terminating"}
            ),
            None,
        )


def prepare_unit(compute: ComputeServices, *, desired: int) -> ComputeUnitRecord:
    return compute.provisioning.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=desired,
        root_volume_gib=200,
    )
