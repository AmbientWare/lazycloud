from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from math import ceil
from uuid import NAMESPACE_URL, uuid4, uuid5

from database.repositories.compute import (
    ComputeCapacityOperationRecord,
    ComputeCapacityOperationRepository,
    ComputeCapacityRequestRecord,
    ComputeCapacityRequestRepository,
    ComputeJoinCredentialRecord,
    ComputeJoinCredentialRepository,
    ComputeLedgerRecord,
    ComputeLedgerRepository,
    ComputePoolRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeSolverDecisionRepository,
    ComputeSolverRunRecord,
    ComputeSolverRunRepository,
    WorkspaceComputePolicyRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.observability import UsageRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    PoolRepository,
    WorkerRepository,
)
from database.types import DatabaseSession
from foundation.ids import optional_uuid, try_uuid
from observability.usage_exporter import UsageMetricsExporter
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import Field, JsonValue, TypeAdapter
from shared.app_identity import NAME
from shared.capacity import (
    CapacityAcquisitionRequest,
    CapacityAcquisitionResult,
    CapacityAcquisitionShape,
    CapacityAcquisitionStatus,
    CapacityFailureCode,
    CapacityOwnerKind,
    CapacityOwnerSource,
    CapacityPoolSizingState,
    CapacityPoolSizingStateUpdate,
    CapacityReleaseRequest,
    capacity_failure_message,
    capacity_owner_for_provider,
)
from shared.compute_enrollment import (
    ComputeCredentialStatus,
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
)
from shared.compute_fleet import Machine, Pool, ResourceStatus, Worker
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputePoolPhase,
    ComputePoolRecord,
    ComputePoolVisibility,
    ComputeResourceRequirements,
)
from shared.containers import ContainerStatus
from shared.contracts import ContractModel
from shared.errors import (
    CapacityLimitReachedError,
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import WorkspaceStatus
from shared.timestamps import utc_now
from shared.usage import UsageMetric, UsageUnit, usage_record_id

from compute.agent_control import (
    ComputePrincipal,
    JoinTokenCreationPlan,
    plan_join_token_creation,
)
from compute.aws_connections import AwsAccountPoolDrain
from compute.billing import (
    BillingCreditRequest,
    BillingDecision,
    ManagedComputeBillingClient,
    ManagedUsage,
    NoopManagedComputeBilling,
    managed_cost_cents,
)
from compute.context import ComputeContext
from compute.offers import (
    ComputeDemand,
    ComputeOffer,
    ComputeReservation,
    ComputeSolveAction,
    ComputeSolvePlan,
    OfferRequest,
    ReservationSource,
    ReservationStatus,
    SolveActionType,
    choose_offer,
    solve_compute_capacity,
)
from compute.projection import (
    ComputePoolPlan,
    ComputePoolSource,
    PoolConfig,
    PrivatePoolState,
    ProviderReservation,
    compute_pool_from_config,
    dollars_to_micros,
    parse_ttl_seconds,
    validate_pool_resource_compatibility,
)
from compute.provider_machines import (
    _LAUNCH_STATE_COMMITTED,
    _LAUNCH_STATE_COMPENSATING,
    _LAUNCH_STATE_INTENT,
    LaunchedProviderInstance,
    PreparedProviderLaunch,
    ProviderMachineReconciler,
    ProviderPoolBootstrapFactory,
    _metadata_time,
    _pool_config_int,
    _provider_instance_metadata,
    _provider_launch_state,
    _provider_storage_volume_ids,
    _provider_zero_capacity_converged,
    _require_internal_pooled_pool,
    _reservation_open,
    _reservation_status_from_provider,
    _utc,
    _whole_hours,
    _zero_sizing_state_update,
)
from compute.providers import (
    CapacityOwnerMutationLease,
    ComputeProviderResolver,
    ComputeSchedulerHooks,
    DirectMachineLaunchRequest,
    DirectMachineProvider,
    DirectMachineProviderRegistry,
    PooledCapacityProvider,
    ProviderMachineReference,
    ProviderMachineStatus,
    ProviderPoolRequest,
    ProviderPoolSnapshot,
    ResolvedComputeProvider,
    internal_pool_identity,
)
from compute.reclaim import ComputeReclaimPolicy
from compute.source_cache_storage import SourceCacheStorageLifecycleService

LOGGER = logging.getLogger(__name__)

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class _CapacityRequestMetadata(ContractModel):
    selector: str = ""
    providers: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    gpu: list[str] = Field(default_factory=list)


class ManagedComputeLaunchError(DomainError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        decision: BillingDecision | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.decision = decision or BillingDecision(ok=False, message=message)


_LAUNCH_CODES_UPSTREAM = frozenset({"provider_unavailable"})


def _launch_failure_as_domain_error(exc: ManagedComputeLaunchError) -> DomainError:
    """Map a launch refusal to the status its code deserves.

    Only an unreachable provider is a 503. A quota, an empty offer set or an
    exhausted balance are states the caller owns and can act on, and reporting
    them as an upstream outage tells them to wait for something that will not
    change on its own.
    """
    if exc.code in _LAUNCH_CODES_UPSTREAM:
        return UpstreamUnavailableError(exc.message, code=exc.code)
    return ConflictError(exc.message, code=exc.code)


@dataclass(frozen=True, slots=True)
class _PooledCapacityBaseline:
    initial_machines: int
    min_machines: int
    min_free_cpu_millicores: int
    min_free_memory_mib: int


_BOOTSTRAP_PHASE_TRANSITIONS: dict[MachineBootstrapPhase, frozenset[MachineBootstrapPhase]] = {
    MachineBootstrapPhase.Requested: frozenset(
        {
            MachineBootstrapPhase.Provisioning,
            MachineBootstrapPhase.Booting,
            MachineBootstrapPhase.Failed,
            MachineBootstrapPhase.Deleting,
        }
    ),
    MachineBootstrapPhase.Provisioning: frozenset(
        {
            MachineBootstrapPhase.Booting,
            MachineBootstrapPhase.Joining,
            MachineBootstrapPhase.Failed,
            MachineBootstrapPhase.Deleting,
        }
    ),
    MachineBootstrapPhase.Booting: frozenset(
        {
            MachineBootstrapPhase.Joining,
            MachineBootstrapPhase.Failed,
            MachineBootstrapPhase.Deleting,
        }
    ),
    MachineBootstrapPhase.Joining: frozenset(
        {
            MachineBootstrapPhase.Failed,
            MachineBootstrapPhase.Deleting,
        }
    ),
    MachineBootstrapPhase.Failed: frozenset({MachineBootstrapPhase.Deleting}),
    MachineBootstrapPhase.Deleting: frozenset(),
}


@dataclass(slots=True)
class ComputeService:
    context: ComputeContext
    provider_registry: DirectMachineProviderRegistry | None = None
    provider_resolver: ComputeProviderResolver | None = None
    pool_bootstrap_factory: ProviderPoolBootstrapFactory | None = None
    billing: ManagedComputeBillingClient = field(default_factory=NoopManagedComputeBilling)
    usage_exporter: UsageMetricsExporter | None = None
    scheduler_hooks: ComputeSchedulerHooks | None = None
    workspace_changes: WorkspaceChangePublisher | None = None
    capacity_owner_mutations: CapacityOwnerMutationLease | None = None
    reclaim: ComputeReclaimPolicy = field(default_factory=ComputeReclaimPolicy)
    source_cache_lifecycle: SourceCacheStorageLifecycleService = field(init=False)
    _stale_first_seen: dict[tuple[str, str, str], datetime] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.source_cache_lifecycle = SourceCacheStorageLifecycleService(self.context)

    @property
    def provider_machines(self) -> ProviderMachineReconciler:
        """Bound to this service's *current* configuration.

        `reclaim` and `scheduler_hooks` are reassigned after construction, so a
        reconciler captured once would answer from stale settings. The stale
        machine ledger is passed by reference because it must survive across
        calls; everything else is read fresh.
        """
        return ProviderMachineReconciler(
            context=self.context,
            reclaim=self.reclaim,
            pool_bootstrap_factory=self.pool_bootstrap_factory,
            workspace_changes=self.workspace_changes,
            scheduler_hooks=self.scheduler_hooks,
            source_cache_lifecycle=self.source_cache_lifecycle,
            stale_first_seen=self._stale_first_seen,
        )

    def record_provider_bootstrap_status(
        self,
        *,
        pool_id: str,
        provider_instance_id: str,
        phase: MachineBootstrapPhase,
        failure_reason: MachineBootstrapFailureReason | None,
        failure_detail: str = "",
        now: datetime | None = None,
    ) -> ComputeProviderInstanceRecord:
        if phase is MachineBootstrapPhase.Failed and failure_reason is None:
            raise InvalidInputError("failed provider bootstrap requires a failure reason")
        if phase is not MachineBootstrapPhase.Failed and failure_reason is not None:
            raise InvalidInputError("provider bootstrap failure reason requires failed phase")
        current_time = _utc(now)
        with self.context.database.session() as session:
            repository = ComputeProviderInstanceRepository(session)
            record = repository.get_for_pool_instance(
                pool_id,
                provider_instance_id,
                for_update=True,
            )
            if record is None:
                raise NotFoundError("provider node is no longer active")
            if phase is not record.bootstrap_phase:
                allowed = _BOOTSTRAP_PHASE_TRANSITIONS[record.bootstrap_phase]
                if phase not in allowed:
                    raise ConflictError(
                        "provider bootstrap phase cannot move from "
                        f"{record.bootstrap_phase.value} to {phase.value}"
                    )
            updated = record.model_copy(
                update={
                    "bootstrap_phase": phase,
                    "bootstrap_failure_reason": failure_reason,
                    "bootstrap_failure_detail": failure_detail,
                    "bootstrap_observed_at": current_time,
                    "updated_at": current_time,
                }
            )
            return repository.upsert(updated)

    def ensure_capacity(
        self,
        request: CapacityAcquisitionRequest,
        *,
        minimum_unit: int = 0,
    ) -> CapacityAcquisitionResult:
        """Resolve the next bounded unit for a reservation and acquire it.

        The unit is derived here rather than supplied, because compute holds the
        authoritative count. The intent is committed with the pool row locked
        before any provider call, and the provider idempotency key is derived
        from the operation id, so a crash between the two cannot double-buy.

        The operation id is deliberately confined to this compute boundary. Public
        SDK and HTTP contracts expose the scheduler reservation, never provider
        mutation identity.
        """
        planned = self._plan_capacity_acquisition(request)
        if planned.status is not CapacityAcquisitionStatus.Requested:
            return planned
        # Pool sizing asks for a floor it has already committed to; a reservation
        # asks for nothing and takes the planned next unit.
        desired_unit = max(planned.desired_unit, minimum_unit)
        with self.context.database.session() as session:
            pool = ComputePoolRepository(session).get_by_capacity_owner_id(
                request.capacity_owner_id
            )
        if pool is None:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                desired_unit=desired_unit,
                reason="capacity owner is not managed by compute",
            )
        if pool.capacity_owner_kind is CapacityOwnerKind.ManagedPool:
            return self._acquire_direct_capacity(pool, request, desired_unit=desired_unit)
        if pool.capacity_owner_kind is CapacityOwnerKind.PooledProvider:
            return self._acquire_pooled_capacity(pool, request, desired_unit=desired_unit)
        return _capacity_result(
            request,
            CapacityAcquisitionStatus.Unsupported,
            desired_unit=desired_unit,
            reason=f"capacity owner kind {pool.capacity_owner_kind.value!r} is unsupported",
        )

    def _plan_capacity_acquisition(
        self,
        request: CapacityAcquisitionRequest,
    ) -> CapacityAcquisitionResult:
        """Read authoritative capacity and plan the exact next bounded unit.

        Existing operation intent always wins so retries cannot advance capacity twice.
        """
        with self.context.database.session() as session:
            pool = ComputePoolRepository(session).get_by_capacity_owner_id(
                request.capacity_owner_id
            )
            operation = ComputeCapacityOperationRepository(session).get(
                request.capacity_owner_id,
                request.operation_id,
            )
            policy = (
                PoolRepository(session).get(pool.name, workspace_id=pool.workspace_id)
                if pool is not None
                else None
            )
            direct_units = (
                len(
                    [
                        record
                        for record in ComputeProviderInstanceRepository(session).list_for_pool(
                            pool.id
                        )
                        if _reservation_open(record.status)
                    ]
                )
                if pool is not None
                else 0
            )
        if pool is None or policy is None:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                desired_unit=1,
                reason="capacity owner is not managed by compute",
            )
        degraded_reason = pool.provider_state.degraded_reason
        if degraded_reason is not None:
            # The pool exhausted its launch attempts. Only the reconciler path
            # used to honour this, so acquisition kept buying machines that
            # could not become workers — a bounded failure billed as an
            # unbounded one. Clearing it is an explicit operator mutation.
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                desired_unit=max(direct_units, 1),
                reason=degraded_reason,
            )
        if not policy.scaling_enabled:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                desired_unit=max(direct_units, 1),
                reason="capacity owner scaling is disabled",
            )
        if not _shape_matches_pool(request.shape, policy):
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                desired_unit=max(direct_units, 1),
                reason="requested unit does not match the capacity owner's fixed worker shape",
            )
        if operation is not None:
            _validate_capacity_operation_plan(operation, request)
            status = _stored_capacity_status(operation.status)
            if status is CapacityAcquisitionStatus.TemporarilyUnavailable:
                status = CapacityAcquisitionStatus.Requested
            return CapacityAcquisitionResult(
                status=status,
                capacity_owner_id=operation.capacity_owner_id,
                reservation_id=operation.reservation_id,
                desired_unit=operation.desired_unit,
                target_machine_id=operation.target_machine_id,
                reason=operation.last_error,
            )
        if pool.capacity_owner_kind is CapacityOwnerKind.ManagedPool:
            return _plan_next_capacity_unit(
                request,
                current_units=direct_units,
                max_units=policy.max_workers,
            )
        if pool.capacity_owner_kind is CapacityOwnerKind.PooledProvider:
            try:
                current_pool, provider, offer = self._internal_pool_provider(
                    pool.workspace_id,
                    pool.name,
                )
                if provider.pooled is None:
                    raise RuntimeError("capacity owner is not backed by a pooled provider")
                snapshot = provider.pooled.describe_pool(
                    self._provider_pool_request(current_pool, offer)
                )
            except Exception as exc:
                return _capacity_result(
                    request,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    desired_unit=max(pool.desired_machines, 1),
                    reason=capacity_failure_message(
                        CapacityFailureCode.ProviderReconciliationFailed,
                        exception_type=type(exc).__name__,
                    ),
                )
            return _plan_next_capacity_unit(
                request,
                current_units=snapshot.desired_machines,
                max_units=pool.max_machines,
            )
        return _capacity_result(
            request,
            CapacityAcquisitionStatus.Unsupported,
            desired_unit=1,
            reason=f"capacity owner kind {pool.capacity_owner_kind.value!r} is unsupported",
        )

    def release_acquired_capacity(
        self,
        request: CapacityReleaseRequest,
    ) -> CapacityAcquisitionResult:
        with self.context.database.session() as session:
            pool = ComputePoolRepository(session).get_by_capacity_owner_id(
                request.capacity_owner_id
            )
            operation = ComputeCapacityOperationRepository(session).get(
                request.capacity_owner_id,
                request.operation_id,
            )
        if pool is None or operation is None or operation.reservation_id != request.reservation_id:
            return CapacityAcquisitionResult(
                status=CapacityAcquisitionStatus.Unsupported,
                capacity_owner_id=request.capacity_owner_id,
                reservation_id=request.reservation_id,
                desired_unit=max(operation.desired_unit if operation is not None else 1, 1),
                reason="capacity operation is not owned by this reservation",
            )
        if operation.status == "released":
            return _operation_result(operation, CapacityAcquisitionStatus.ExistingPending)
        if not operation.owns_capacity:
            with self.context.database.session() as session:
                repository = ComputeCapacityOperationRepository(session)
                current = repository.get(
                    request.capacity_owner_id,
                    request.operation_id,
                    for_update=True,
                )
                if current is not None:
                    operation = repository.upsert(
                        current.model_copy(update={"status": "released", "updated_at": utc_now()})
                    )
            return _operation_result(operation, CapacityAcquisitionStatus.Requested)
        if pool.capacity_owner_kind is CapacityOwnerKind.ManagedPool:
            return self._release_direct_capacity(pool, operation)
        if pool.capacity_owner_kind is CapacityOwnerKind.PooledProvider:
            return self._release_pooled_capacity(pool, operation)
        return _operation_result(
            operation,
            CapacityAcquisitionStatus.Unsupported,
            reason="capacity owner does not support compute release",
        )

    def _acquire_direct_capacity(
        self,
        pool: ComputePoolRecord,
        request: CapacityAcquisitionRequest,
        *,
        desired_unit: int,
    ) -> CapacityAcquisitionResult:
        with self.context.database.session() as session:
            policy = PoolRepository(session).get(pool.name, workspace_id=pool.workspace_id)
            records = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
            existing_operation = ComputeCapacityOperationRepository(session).get(
                request.capacity_owner_id,
                request.operation_id,
            )
        if policy is None or not _shape_matches_pool(request.shape, policy):
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                reason="requested unit does not match the capacity owner's fixed worker shape",
                desired_unit=desired_unit,
            )
        clients = self._provider_client_snapshot(pool.workspace_id)
        provider_name = pool.provider_ref or policy.provider
        provider = clients.get(provider_name)
        if provider is None:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason=f"direct provider {provider_name!r} is unavailable",
                desired_unit=desired_unit,
            )
        try:
            offer = next(
                (
                    item
                    for item in provider.list_offers()
                    if _offer_matches_capacity_shape(item, request.shape)
                    and _offer_matches_capacity_policy(item, policy)
                ),
                None,
            )
        except Exception as exc:
            return self._record_capacity_failure(
                request,
                reason=capacity_failure_message(
                    CapacityFailureCode.ProviderUnavailable,
                    exception_type=type(exc).__name__,
                ),
                failure_code=CapacityFailureCode.ProviderUnavailable,
                desired_unit=desired_unit,
            )
        if offer is None:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                reason="provider has no offer matching the fixed worker shape",
                desired_unit=desired_unit,
            )
        expected_machine_ids = {
            item.machine_id
            for item in records
            if item.machine_id is not None and _reservation_open(item.status)
        }
        try:
            observed = provider.reconcile_machines(pool.name, expected_machine_ids)
        except Exception as exc:
            return self._record_capacity_failure(
                request,
                reason=capacity_failure_message(
                    CapacityFailureCode.ProviderReconciliationFailed,
                    exception_type=type(exc).__name__,
                ),
                failure_code=CapacityFailureCode.ProviderReconciliationFailed,
                desired_unit=desired_unit,
            )
        observed_by_machine = {item.machine_id: item for item in observed.observed_machines}
        if existing_operation is not None:
            _validate_capacity_operation(existing_operation, request, desired_unit=desired_unit)
            if existing_operation.status == "released":
                return _operation_result(
                    existing_operation,
                    CapacityAcquisitionStatus.Unsupported,
                    reason="released capacity operation cannot be reacquired",
                )
            if not existing_operation.owns_capacity:
                return _operation_result(
                    existing_operation,
                    _stored_capacity_status(existing_operation.status),
                )
            target = existing_operation.target_machine_id
            if target is None:
                return self._record_capacity_failure(
                    request,
                    reason="direct capacity intent has no target machine",
                    desired_unit=desired_unit,
                )
            remote = observed_by_machine.get(target)
            if remote is not None:
                self._commit_direct_capacity_operation(pool, existing_operation, remote)
                return _operation_result(
                    existing_operation.model_copy(
                        update={
                            "status": CapacityAcquisitionStatus.Requested.value,
                            "provider_instance_id": remote.provider_instance_id,
                        }
                    ),
                    CapacityAcquisitionStatus.ExistingPending,
                )

        with self.context.database.session() as session:
            pools = ComputePoolRepository(session)
            locked_pool = pools.get(pool.id, for_update=True)
            if locked_pool is None:
                return _capacity_result(
                    request,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason="capacity owner disappeared during acquisition",
                    desired_unit=desired_unit,
                )
            operations = ComputeCapacityOperationRepository(session)
            operation = operations.get(
                request.capacity_owner_id,
                request.operation_id,
                for_update=True,
            )
            if operation is None:
                open_records = [
                    item
                    for item in ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
                    if _reservation_open(item.status)
                ]
                current_units = len(open_records)
                if desired_unit > policy.max_workers:
                    operation = operations.upsert(
                        _new_capacity_operation(
                            pool,
                            request,
                            desired_unit=desired_unit,
                            status=CapacityAcquisitionStatus.AtLimit.value,
                            previous_desired_unit=current_units,
                        )
                    )
                    return _operation_result(operation, CapacityAcquisitionStatus.AtLimit)
                if desired_unit <= current_units:
                    operation = operations.upsert(
                        _new_capacity_operation(
                            pool,
                            request,
                            desired_unit=desired_unit,
                            status=CapacityAcquisitionStatus.ExistingPending.value,
                            previous_desired_unit=current_units,
                        )
                    )
                    return _operation_result(operation, CapacityAcquisitionStatus.ExistingPending)
                if desired_unit != current_units + 1:
                    operation = operations.upsert(
                        _new_capacity_operation(
                            pool,
                            request,
                            desired_unit=desired_unit,
                            status=CapacityAcquisitionStatus.TemporarilyUnavailable.value,
                            previous_desired_unit=current_units,
                            last_error="desired unit skips authoritative direct capacity",
                        )
                    )
                    return _operation_result(
                        operation,
                        CapacityAcquisitionStatus.TemporarilyUnavailable,
                        reason=operation.last_error,
                    )
                machine_id = str(uuid5(NAMESPACE_URL, f"capacity-machine\0{request.operation_id}"))
                machine = MachineRepository(session).get_across_workspaces(machine_id)
                if machine is None:
                    machine = MachineRepository(session).records.create(
                        {
                            "id": machine_id,
                            "pool": pool.name,
                            "provider": provider_name,
                            "cpu": offer.cpu_millicores / 1000,
                            "memory": f"{offer.memory_mb}Mi",
                            "gpu": offer.gpu,
                            "labels": {
                                "source": "capacity_acquisition",
                                "offer_id": offer.id,
                            },
                            "status": ResourceStatus.Created.value,
                        },
                        workspace_id=pool.workspace_id,
                        status=ResourceStatus.Created.value,
                    )
                operation = operations.upsert(
                    _new_capacity_operation(
                        pool,
                        request,
                        desired_unit=desired_unit,
                        status=_LAUNCH_STATE_INTENT,
                        previous_desired_unit=current_units,
                        target_machine_id=machine.id,
                        owns_capacity=True,
                    )
                )
                provider_record_id = str(
                    uuid5(NAMESPACE_URL, f"capacity-provider-record\0{request.operation_id}")
                )
                provider_records = ComputeProviderInstanceRepository(session)
                if provider_records.records.get(provider_record_id) is None:
                    provider_records.records.create(
                        {
                            "id": provider_record_id,
                            "pool_id": pool.id,
                            "provider": provider_name,
                            "offer_id": offer.id,
                            "instance_type": offer.instance_type,
                            "instance_id": f"intent:{machine.id}",
                            "machine_id": machine.id,
                            "gpu": offer.gpu,
                            "gpu_count": offer.gpu_count,
                            "cpu_millicores": offer.cpu_millicores,
                            "memory_mb": offer.memory_mb,
                            "hourly_cost_micros": offer.hourly_cost_micros,
                            "source": "capacity_acquisition",
                            "status": ReservationStatus.Pending.value,
                            "metadata": {
                                "launch_state": _LAUNCH_STATE_INTENT,
                            },
                        },
                        status=ReservationStatus.Pending.value,
                    )
            else:
                _validate_capacity_operation(operation, request, desired_unit=desired_unit)
            if operation.target_machine_id is None:
                operation = operations.upsert(
                    operation.model_copy(
                        update={
                            "status": CapacityAcquisitionStatus.TemporarilyUnavailable.value,
                            "last_error": "direct capacity intent has no target machine",
                            "updated_at": utc_now(),
                        }
                    )
                )
                return _operation_result(
                    operation,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                )
            target_machine_id = operation.target_machine_id
            provider_record = ComputeProviderInstanceRepository(session).get_by_machine(
                target_machine_id
            )
            if provider_record is None:
                raise RuntimeError("direct provider capacity intent disappeared")
            workspace_record = self.context.workspace(session, pool.workspace_id)
            if workspace_record.signing_key == "":
                return _operation_result(
                    operation,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason="workspace signing authority is unavailable",
                )
            principal = ComputePrincipal(workspace_id=pool.workspace_id, owner_token_id=NAME)
            credentials = ComputeJoinCredentialRepository(session)
            join_token = _plan_capacity_join_token(
                workspace_record.signing_key,
                principal=principal,
                pool_name=pool.name,
                operation_id=request.operation_id,
                machine_id=target_machine_id,
                join_attempt=operation.join_attempt,
            )
            credential = credentials.get_by_hash(join_token.token_hash, for_update=True)
            reuse = _capacity_join_credential_reuse(
                credential,
                workspace_id=pool.workspace_id,
                pool_name=pool.name,
                machine_id=target_machine_id,
                now=utc_now(),
            )
            if reuse is _CapacityJoinReuse.Renew:
                # The previous attempt was compensated: its authority is revoked or
                # expired and was never used. Advance the attempt so both the join
                # token and the provider idempotency key are new, instead of retrying
                # forever against a hash that can only ever re-derive dead authority.
                operation = operations.upsert(
                    operation.model_copy(
                        update={
                            "join_attempt": operation.join_attempt + 1,
                            "updated_at": utc_now(),
                        }
                    )
                )
                join_token = _plan_capacity_join_token(
                    workspace_record.signing_key,
                    principal=principal,
                    pool_name=pool.name,
                    operation_id=request.operation_id,
                    machine_id=target_machine_id,
                    join_attempt=operation.join_attempt,
                )
                credential = credentials.get_by_hash(join_token.token_hash, for_update=True)
                reuse = _capacity_join_credential_reuse(
                    credential,
                    workspace_id=pool.workspace_id,
                    pool_name=pool.name,
                    machine_id=target_machine_id,
                    now=utc_now(),
                )
            if reuse is not _CapacityJoinReuse.Usable:
                return _operation_result(
                    operation,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason="stable provider join authority is no longer usable",
                )
            if credential is None:
                credentials.create(
                    token_hash=join_token.token_hash,
                    workspace_id=pool.workspace_id,
                    pool_name=pool.name,
                    machine_id=target_machine_id,
                    created_by_token_id=None,
                    max_uses=join_token.state.max_uses,
                    expires_at=join_token.expires_at,
                )
            ComputeProviderInstanceRepository(session).upsert(
                provider_record.model_copy(
                    update={
                        "metadata": {
                            **_provider_instance_metadata(provider_record),
                            "registration_token_hash": join_token.token_hash,
                        }
                    }
                )
            )
        try:
            remote = provider.launch_machine(
                DirectMachineLaunchRequest(
                    workspace_id=pool.workspace_id,
                    pool_name=pool.name,
                    registration_token=join_token.token,
                    machine_id=target_machine_id,
                    operation_id=request.operation_id,
                    idempotency_key=_capacity_launch_idempotency_key(
                        operation_id=request.operation_id,
                        join_attempt=operation.join_attempt,
                    ),
                    offer=offer,
                )
            )
        except Exception as exc:
            return self._record_capacity_failure(
                request,
                reason=capacity_failure_message(
                    CapacityFailureCode.ProviderLaunchFailed,
                    exception_type=type(exc).__name__,
                ),
                failure_code=CapacityFailureCode.ProviderLaunchFailed,
                desired_unit=desired_unit,
            )
        self._commit_direct_capacity_operation(pool, operation, remote)
        return _operation_result(
            operation.model_copy(
                update={
                    "status": CapacityAcquisitionStatus.Requested.value,
                    "provider_instance_id": remote.provider_instance_id,
                }
            ),
            CapacityAcquisitionStatus.Requested,
        )

    def _commit_direct_capacity_operation(
        self,
        pool: ComputePoolRecord,
        operation: ComputeCapacityOperationRecord,
        remote: ProviderMachineReference,
    ) -> None:
        if operation.target_machine_id is None:
            raise RuntimeError("direct capacity operation has no target machine")
        with self.context.database.session() as session:
            operations = ComputeCapacityOperationRepository(session)
            current = operations.get(
                operation.capacity_owner_id,
                operation.operation_id,
                for_update=True,
            )
            record = ComputeProviderInstanceRepository(session).get_by_machine(
                operation.target_machine_id
            )
            machine = MachineRepository(session).get_across_workspaces(operation.target_machine_id)
            if current is None or record is None or machine is None:
                raise RuntimeError("direct capacity intent disappeared after provider launch")
            ComputeProviderInstanceRepository(session).upsert(
                record.model_copy(
                    update={
                        "instance_id": remote.provider_instance_id,
                        "status": _reservation_status_from_provider(remote.status).value,
                        "metadata": {
                            **_provider_instance_metadata(record),
                            "launch_state": _LAUNCH_STATE_COMMITTED,
                            "storage_volume_ids": list(remote.storage_volume_ids),
                        },
                        "updated_at": utc_now(),
                    }
                )
            )
            self._update_machine_after_launch(
                session,
                machine,
                provider_instance_id=remote.provider_instance_id,
                status=remote.status,
                address=remote.address,
            )
            operations.upsert(
                current.model_copy(
                    update={
                        "status": CapacityAcquisitionStatus.Requested.value,
                        "provider_instance_id": remote.provider_instance_id,
                        "last_error": "",
                        "updated_at": utc_now(),
                    }
                )
            )

    def _acquire_pooled_capacity(
        self,
        pool: ComputePoolRecord,
        request: CapacityAcquisitionRequest,
        *,
        desired_unit: int,
    ) -> CapacityAcquisitionResult:
        try:
            current_pool, provider, offer = self._internal_pool_provider(
                pool.workspace_id,
                pool.name,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason=capacity_failure_message(
                    CapacityFailureCode.Unknown,
                    exception_type=type(exc).__name__,
                ),
                failure_code=CapacityFailureCode.Unknown,
                desired_unit=desired_unit,
            )
        if provider.pooled is None:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                reason="capacity owner is not backed by a pooled provider",
                desired_unit=desired_unit,
            )
        if not _offer_matches_capacity_shape(offer, request.shape):
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                reason="requested unit does not match the capacity owner's fixed worker shape",
                desired_unit=desired_unit,
            )
        provider_request = self._provider_pool_request(current_pool, offer)
        try:
            snapshot = provider.pooled.describe_pool(provider_request)
        except Exception as exc:
            return self._record_capacity_failure(
                request,
                reason=capacity_failure_message(
                    CapacityFailureCode.ProviderReconciliationFailed,
                    exception_type=type(exc).__name__,
                ),
                failure_code=CapacityFailureCode.ProviderReconciliationFailed,
                desired_unit=desired_unit,
            )
        with self.context.database.session() as session:
            pools = ComputePoolRepository(session)
            locked_pool = pools.get(current_pool.id, for_update=True)
            if locked_pool is None:
                return _capacity_result(
                    request,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason="capacity owner disappeared during acquisition",
                    desired_unit=desired_unit,
                )
            operations = ComputeCapacityOperationRepository(session)
            operation = operations.get(
                request.capacity_owner_id,
                request.operation_id,
                for_update=True,
            )
            if operation is not None:
                _validate_capacity_operation(operation, request, desired_unit=desired_unit)
                if operation.status == "released":
                    return _operation_result(
                        operation,
                        CapacityAcquisitionStatus.Unsupported,
                        reason="released capacity operation cannot be reacquired",
                    )
                if not operation.owns_capacity:
                    return _operation_result(
                        operation,
                        _stored_capacity_status(operation.status),
                    )
            else:
                current_units = snapshot.desired_machines
                if desired_unit > locked_pool.max_machines:
                    operation = operations.upsert(
                        _new_capacity_operation(
                            locked_pool,
                            request,
                            desired_unit=desired_unit,
                            status=CapacityAcquisitionStatus.AtLimit.value,
                            previous_desired_unit=current_units,
                        )
                    )
                    return _operation_result(operation, CapacityAcquisitionStatus.AtLimit)
                if desired_unit <= current_units:
                    operation = operations.upsert(
                        _new_capacity_operation(
                            locked_pool,
                            request,
                            desired_unit=desired_unit,
                            status=CapacityAcquisitionStatus.ExistingPending.value,
                            previous_desired_unit=current_units,
                        )
                    )
                    return _operation_result(operation, CapacityAcquisitionStatus.ExistingPending)
                if desired_unit != current_units + 1:
                    operation = operations.upsert(
                        _new_capacity_operation(
                            locked_pool,
                            request,
                            desired_unit=desired_unit,
                            status=CapacityAcquisitionStatus.TemporarilyUnavailable.value,
                            previous_desired_unit=current_units,
                            last_error="desired unit skips authoritative pooled capacity",
                        )
                    )
                    return _operation_result(
                        operation,
                        CapacityAcquisitionStatus.TemporarilyUnavailable,
                        reason=operation.last_error,
                    )
                operation = operations.upsert(
                    _new_capacity_operation(
                        locked_pool,
                        request,
                        desired_unit=desired_unit,
                        status=_LAUNCH_STATE_INTENT,
                        previous_desired_unit=current_units,
                        owns_capacity=True,
                    )
                )
        if snapshot.desired_machines >= desired_unit:
            with self.context.database.session() as session:
                repository = ComputeCapacityOperationRepository(session)
                current = repository.get(
                    request.capacity_owner_id,
                    request.operation_id,
                    for_update=True,
                )
                if current is not None:
                    operation = repository.upsert(
                        current.model_copy(
                            update={
                                "status": CapacityAcquisitionStatus.Requested.value,
                                "last_error": "",
                                "updated_at": utc_now(),
                            }
                        )
                    )
            return _operation_result(operation, CapacityAcquisitionStatus.ExistingPending)
        try:
            updated_snapshot = provider.pooled.set_pool_capacity(
                provider_request,
                desired_machines=desired_unit,
                max_machines=current_pool.max_machines,
            )
            self.provider_machines._apply_pooled_snapshot(
                current_pool.model_copy(update={"desired_machines": desired_unit}),
                offer,
                updated_snapshot,
                provider=provider.pooled,
                update_capacity=True,
            )
        except Exception as exc:
            return self._record_capacity_failure(
                request,
                reason=capacity_failure_message(
                    CapacityFailureCode.CapacityPlanningFailed,
                    exception_type=type(exc).__name__,
                ),
                failure_code=CapacityFailureCode.CapacityPlanningFailed,
                desired_unit=desired_unit,
            )
        with self.context.database.session() as session:
            repository = ComputeCapacityOperationRepository(session)
            current = repository.get(
                request.capacity_owner_id,
                request.operation_id,
                for_update=True,
            )
            if current is None:
                raise RuntimeError("pooled capacity operation disappeared")
            operation = repository.upsert(
                current.model_copy(
                    update={
                        "status": CapacityAcquisitionStatus.Requested.value,
                        "last_error": "",
                        "updated_at": utc_now(),
                    }
                )
            )
        return _operation_result(operation, CapacityAcquisitionStatus.Requested)

    def _release_direct_capacity(
        self,
        pool: ComputePoolRecord,
        operation: ComputeCapacityOperationRecord,
    ) -> CapacityAcquisitionResult:
        if operation.target_machine_id is None:
            return _operation_result(
                operation,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason="direct capacity operation has no target machine",
            )
        clients = self._provider_client_snapshot(pool.workspace_id)
        with self.context.database.session() as session:
            operations = ComputeCapacityOperationRepository(session)
            current = operations.get(
                operation.capacity_owner_id,
                operation.operation_id,
                for_update=True,
            )
            record = ComputeProviderInstanceRepository(session).get_by_machine(
                operation.target_machine_id
            )
            if current is None or record is None:
                return _operation_result(
                    operation,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason="owned direct capacity record is unavailable",
                )
            current = operations.upsert(
                current.model_copy(update={"status": "releasing", "updated_at": utc_now()})
            )
            if not _reservation_open(record.status):
                client = clients.get(record.provider)
                provider_instance_id = record.instance_id or record.id
                try:
                    destroyed = (
                        record.status == ReservationStatus.Deleted.value
                        and client is not None
                        and client.machine_storage_destroyed(
                            provider_instance_id,
                            _provider_storage_volume_ids(record),
                        )
                    )
                except Exception:
                    LOGGER.warning(
                        "could not confirm storage destruction for %s",
                        provider_instance_id,
                        exc_info=True,
                    )
                    destroyed = False
            else:
                destroyed = self.provider_machines._terminate_provider_record(
                    session,
                    record,
                    clients=clients,
                    reason="capacity_reservation_released",
                    message="capacity reservation released its owned machine",
                )
            if destroyed:
                current = operations.upsert(
                    current.model_copy(
                        update={"status": "released", "last_error": "", "updated_at": utc_now()}
                    )
                )
                return _operation_result(current, CapacityAcquisitionStatus.Requested)
            current = operations.upsert(
                current.model_copy(
                    update={
                        "last_error": "provider has not confirmed machine storage destruction",
                        "updated_at": utc_now(),
                    }
                )
            )
            return _operation_result(
                current,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason=current.last_error,
            )

    def _release_pooled_capacity(
        self,
        pool: ComputePoolRecord,
        operation: ComputeCapacityOperationRecord,
    ) -> CapacityAcquisitionResult:
        try:
            current_pool, provider, offer = self._internal_pool_provider(
                pool.workspace_id,
                pool.name,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            return _operation_result(
                operation,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason=capacity_failure_message(
                    CapacityFailureCode.Unknown,
                    exception_type=type(exc).__name__,
                ),
                failure_code=CapacityFailureCode.Unknown,
            )
        if provider.pooled is None:
            return _operation_result(
                operation,
                CapacityAcquisitionStatus.Unsupported,
                reason="capacity owner is not backed by a pooled provider",
            )
        provider_request = self._provider_pool_request(current_pool, offer)
        try:
            snapshot = provider.pooled.describe_pool(provider_request)
        except Exception as exc:
            return _operation_result(
                operation,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason=capacity_failure_message(
                    CapacityFailureCode.ProviderReconciliationFailed,
                    exception_type=type(exc).__name__,
                ),
                failure_code=CapacityFailureCode.ProviderReconciliationFailed,
            )
        with self.context.database.session() as session:
            pools = ComputePoolRepository(session)
            if pools.get(current_pool.id, for_update=True) is None:
                return _operation_result(
                    operation,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason="capacity owner disappeared during release",
                )
            operations = ComputeCapacityOperationRepository(session)
            current = operations.get(
                operation.capacity_owner_id,
                operation.operation_id,
                for_update=True,
            )
            if current is None:
                return _operation_result(
                    operation,
                    CapacityAcquisitionStatus.Unsupported,
                    reason="capacity operation disappeared during release",
                )
            all_operations = operations.list_for_owner(operation.capacity_owner_id)
            base_units = min(
                (item.previous_desired_unit for item in all_operations if item.owns_capacity),
                default=current.previous_desired_unit,
            )
            active_owned = sum(
                1
                for item in all_operations
                if item.owns_capacity
                and item.operation_id != current.operation_id
                and item.status != "released"
            )
            release_target = current.release_desired_unit
            if release_target is None:
                release_target = max(current_pool.min_machines, base_units + active_owned)
            current = operations.upsert(
                current.model_copy(
                    update={
                        "status": "releasing",
                        "release_desired_unit": release_target,
                        "updated_at": utc_now(),
                    }
                )
            )
        if (
            snapshot.desired_machines <= release_target
            and snapshot.observed_machines <= release_target
        ):
            try:
                self.provider_machines._apply_pooled_snapshot(
                    current_pool.model_copy(update={"desired_machines": release_target}),
                    offer,
                    snapshot,
                    provider=provider.pooled,
                    update_capacity=True,
                )
            except Exception as exc:
                return _operation_result(
                    current,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason=capacity_failure_message(
                        CapacityFailureCode.ProviderReconciliationFailed,
                        exception_type=type(exc).__name__,
                    ),
                    failure_code=CapacityFailureCode.ProviderReconciliationFailed,
                )
            with self.context.database.session() as session:
                operations = ComputeCapacityOperationRepository(session)
                locked = operations.get(
                    operation.capacity_owner_id,
                    operation.operation_id,
                    for_update=True,
                )
                if locked is not None:
                    current = operations.upsert(
                        locked.model_copy(
                            update={"status": "released", "last_error": "", "updated_at": utc_now()}
                        )
                    )
            return _operation_result(current, CapacityAcquisitionStatus.Requested)
        try:
            updated_snapshot = provider.pooled.set_pool_capacity(
                provider_request,
                desired_machines=release_target,
                max_machines=current_pool.max_machines,
            )
            self.provider_machines._apply_pooled_snapshot(
                current_pool.model_copy(update={"desired_machines": release_target}),
                offer,
                updated_snapshot,
                provider=provider.pooled,
                update_capacity=True,
            )
        except Exception as exc:
            return _operation_result(
                current,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason=capacity_failure_message(
                    CapacityFailureCode.ProviderUnavailable,
                    exception_type=type(exc).__name__,
                ),
                failure_code=CapacityFailureCode.ProviderUnavailable,
            )
        if (
            updated_snapshot.desired_machines > release_target
            or updated_snapshot.observed_machines > release_target
        ):
            return _operation_result(
                current,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason="provider has not confirmed owned pooled capacity release",
            )
        with self.context.database.session() as session:
            operations = ComputeCapacityOperationRepository(session)
            locked = operations.get(
                operation.capacity_owner_id,
                operation.operation_id,
                for_update=True,
            )
            if locked is None:
                raise RuntimeError("pooled capacity operation disappeared after release")
            current = operations.upsert(
                locked.model_copy(
                    update={"status": "released", "last_error": "", "updated_at": utc_now()}
                )
            )
        return _operation_result(current, CapacityAcquisitionStatus.Requested)

    def _record_capacity_failure(
        self,
        request: CapacityAcquisitionRequest,
        *,
        desired_unit: int,
        reason: str,
        failure_code: CapacityFailureCode = CapacityFailureCode.Unknown,
    ) -> CapacityAcquisitionResult:
        with self.context.database.session() as session:
            repository = ComputeCapacityOperationRepository(session)
            operation = repository.get(
                request.capacity_owner_id,
                request.operation_id,
                for_update=True,
            )
            if operation is not None:
                operation = repository.upsert(
                    operation.model_copy(
                        update={
                            "status": CapacityAcquisitionStatus.TemporarilyUnavailable.value,
                            "last_error": reason,
                            "failure_code": failure_code,
                            "updated_at": utc_now(),
                        }
                    )
                )
                return _operation_result(
                    operation,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason=reason,
                    failure_code=failure_code,
                )
        return _capacity_result(
            request,
            CapacityAcquisitionStatus.TemporarilyUnavailable,
            reason=reason,
            failure_code=failure_code,
            desired_unit=desired_unit,
        )

    def create_pool(
        self,
        name: str,
        *,
        workspace: str = "default",
        provider: str = "local",
        capacity_owner_id: str | None = None,
        initial_workers: int = 0,
        min_workers: int = 0,
        max_workers: int = 1,
        scaling_enabled: bool = False,
        default_eligible: bool = False,
        priority: int = 0,
        min_free_cpu_millicores: int = 0,
        min_free_memory_mib: int = 0,
        min_free_gpu_count: int = 0,
        worker_cpu_millicores: int = 0,
        worker_memory_mib: int = 0,
        worker_gpu_type: str = "",
        worker_gpu_count: int = 0,
        worker_runtimes: tuple[str, ...] = ("runc",),
        worker_preemptible: bool = False,
        idle_drain_timeout_seconds: int = 300,
        scale_up_cooldown_seconds: int = 5,
        scale_down_cooldown_seconds: int = 60,
        registration_timeout_seconds: int = 600,
        labels: dict[str, str] | None = None,
    ) -> Pool:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = PoolRepository(session)
            existing = repository.get(name, workspace_id=workspace_id)
            if existing is not None and existing.provider != provider:
                raise ConflictError(f"compute pool provider is immutable: {name}")
            if (
                existing is not None
                and capacity_owner_id is not None
                and existing.capacity_owner_id != capacity_owner_id
            ):
                raise ConflictError(f"compute pool capacity owner is immutable: {name}")
            owner_kind, owner_source = capacity_owner_for_provider(provider)
            pool = Pool(
                capacity_owner_id=(
                    existing.capacity_owner_id
                    if existing is not None
                    else capacity_owner_id or str(uuid4())
                ),
                capacity_owner_kind=(
                    existing.capacity_owner_kind if existing is not None else owner_kind
                ),
                capacity_owner_source=(
                    existing.capacity_owner_source if existing is not None else owner_source
                ),
                name=name,
                provider=provider,
                initial_workers=initial_workers,
                min_workers=min_workers,
                max_workers=max_workers,
                scaling_enabled=scaling_enabled,
                default_eligible=default_eligible,
                priority=priority,
                min_free_cpu_millicores=min_free_cpu_millicores,
                min_free_memory_mib=min_free_memory_mib,
                min_free_gpu_count=min_free_gpu_count,
                worker_cpu_millicores=worker_cpu_millicores,
                worker_memory_mib=worker_memory_mib,
                worker_gpu_type=worker_gpu_type,
                worker_gpu_count=worker_gpu_count,
                worker_runtimes=worker_runtimes,
                worker_preemptible=worker_preemptible,
                idle_drain_timeout_seconds=idle_drain_timeout_seconds,
                scale_up_cooldown_seconds=scale_up_cooldown_seconds,
                scale_down_cooldown_seconds=scale_down_cooldown_seconds,
                registration_timeout_seconds=registration_timeout_seconds,
                labels=labels or {},
            )
            saved = repository.upsert(pool, workspace_id=workspace_id)
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputePools,
            change=(
                WorkspaceChangeType.Created if existing is None else WorkspaceChangeType.Updated
            ),
            resource_id=name,
        )
        return saved

    def list_pools(self, *, workspace: str = "default") -> list[Pool]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = PoolRepository(session).list(workspace_id=workspace_id)
        records.sort(key=lambda item: item.name)
        return records

    def list_pools_across_workspaces(self) -> list[tuple[str, Pool]]:
        with self.context.database.session() as session:
            records = PoolRepository(session).list_across_workspaces_with_workspace()
        return sorted(records, key=lambda item: (item[0], item[1].name))

    def get_pool_sizing_state(self, capacity_owner_id: str) -> CapacityPoolSizingState:
        with self.context.database.session() as session:
            state = PoolRepository(session).get_sizing_state(capacity_owner_id)
        if state is None:
            raise ConflictError(f"compute pool capacity owner does not exist: {capacity_owner_id}")
        return state

    def compare_and_set_pool_sizing_state(
        self,
        update: CapacityPoolSizingStateUpdate,
    ) -> CapacityPoolSizingState:
        with self.context.database.session() as session:
            state = PoolRepository(session).compare_and_set_sizing_state(update)
        if state is None:
            raise ConflictError(
                f"compute pool sizing state changed: {update.capacity_owner_id} "
                f"revision {update.expected_revision}"
            )
        return state

    def list_pools_for_workspace_deletion(self, workspace_id: str) -> list[Pool]:
        with self.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            records = PoolRepository(session).list(workspace_id=workspace_id)
        records.sort(key=lambda item: item.name)
        return records

    def delete_pool(self, name: str, *, workspace: str = "default") -> None:
        termination_errors: list[str] = []
        deleted_machine_ids: list[str] = []
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
        clients = self._provider_client_snapshot(workspace_id)
        with self.context.database.session() as session:
            compute_pool_repository = ComputePoolRepository(session)
            compute_pool = compute_pool_repository.get_by_name(workspace_id, name)
            if compute_pool is not None:
                provider_instances = ComputeProviderInstanceRepository(session)
                current_time = utc_now()
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if not _reservation_open(record.status):
                        continue
                    self._record_managed_usage(
                        session,
                        workspace_id=workspace_id,
                        pool_name=name,
                        record=record,
                        now=current_time,
                    )
                    self.provider_machines._terminate_provider_record(
                        session,
                        record,
                        clients=clients,
                        reason="pool_deleted",
                        message="managed compute pool deleted",
                    )
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if not _reservation_open(record.status):
                        continue
                    detail = str(
                        _provider_instance_metadata(record).get("last_error")
                        or "provider unavailable"
                    )
                    termination_errors.append(f"{record.provider}/{record.id}: {detail}")
                if not termination_errors:
                    compute_pool_repository.records.delete(
                        compute_pool.id,
                        workspace_id=workspace_id,
                    )
            if not termination_errors:
                machine_repository = MachineRepository(session)
                for machine in machine_repository.records.list(workspace_id=workspace_id):
                    if machine.pool != name or machine.status is ResourceStatus.Deleted:
                        continue
                    machine_repository.upsert(
                        machine.model_copy(update={"status": ResourceStatus.Deleted}),
                        workspace_id=workspace_id,
                    )
                    deleted_machine_ids.append(machine.id)
                PoolRepository(session).records.delete(name, workspace_id=workspace_id)
        if termination_errors:
            details = "; ".join(termination_errors)
            raise UpstreamUnavailableError(
                f"managed compute pool capacity could not be terminated: {details}"
            )
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputePools,
            change=WorkspaceChangeType.Deleted,
            resource_id=name,
        )
        for machine_id in deleted_machine_ids:
            self._publish_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputeMachines,
                change=WorkspaceChangeType.Deleted,
                resource_id=machine_id,
            )

    def delete_pool_for_workspace_deletion(self, name: str, *, workspace_id: str) -> None:
        """Delete one existing pool without reopening a Deleting workspace."""
        termination_errors: list[str] = []
        clients = self._provider_client_snapshot_for_workspace_deletion(workspace_id)
        with self.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            compute_pool_repository = ComputePoolRepository(session)
            compute_pool = compute_pool_repository.get_by_name(workspace_id, name)
            if compute_pool is not None:
                provider_instances = ComputeProviderInstanceRepository(session)
                current_time = utc_now()
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if not _reservation_open(record.status):
                        continue
                    self._record_managed_usage(
                        session,
                        workspace_id=workspace_id,
                        pool_name=name,
                        record=record,
                        now=current_time,
                        deleting_workspace_id=workspace_id,
                    )
                    self.provider_machines._terminate_provider_record(
                        session,
                        record,
                        clients=clients,
                        reason="workspace_deleted",
                        message="workspace deletion terminated managed compute pool",
                        deleting_workspace_id=workspace_id,
                    )
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if _reservation_open(record.status):
                        detail = str(
                            _provider_instance_metadata(record).get("last_error")
                            or "provider unavailable"
                        )
                        termination_errors.append(f"{record.provider}/{record.id}: {detail}")
            if termination_errors:
                details = "; ".join(termination_errors)
                raise UpstreamUnavailableError(
                    f"managed compute pool capacity could not be terminated: {details}"
                )
            if compute_pool is not None:
                compute_pool_repository.delete_for_workspace_deletion(
                    compute_pool.id,
                    workspace_id=workspace_id,
                )
            machine_repository = MachineRepository(session)
            for machine in machine_repository.records.list(workspace_id=workspace_id):
                if machine.pool != name or machine.status is ResourceStatus.Deleted:
                    continue
                machine_repository.mark_deleted_for_workspace_deletion(
                    machine.id,
                    workspace_id=workspace_id,
                )
            PoolRepository(session).delete_for_workspace_deletion(
                name,
                workspace_id=workspace_id,
            )

    def list_pool_offers(
        self,
        config: PoolConfig,
        *,
        workspace: str = "default",
    ) -> list[ComputeOffer]:
        try:
            request = compute_pool_from_config(
                config,
                node_count=config.nodes,
                require_reservation=False,
            )
            with self.context.database.session() as session:
                workspace_id = self.context.workspace(session, workspace).id
            clients = self._provider_client_snapshot(workspace_id)
            return self._list_pool_offers(request, workspace_id=workspace_id, clients=clients)
        except ManagedComputeLaunchError as exc:
            raise _launch_failure_as_domain_error(exc) from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def _list_pool_offers(
        self,
        request: ComputePoolPlan,
        *,
        workspace_id: str,
        clients: Mapping[str, DirectMachineProvider],
    ) -> list[ComputeOffer]:
        selected_clients = (
            {provider: clients[provider] for provider in request.providers if provider in clients}
            if request.providers
            else dict(sorted(clients.items()))
        )
        resolved = self._resolved_workspace_providers(
            workspace_id,
            request.providers,
            clients=clients,
        )
        missing = [
            provider
            for provider in request.providers
            if provider not in selected_clients and provider not in resolved
        ]
        if missing:
            msg = f"compute provider unavailable: {', '.join(sorted(missing))}"
            raise ManagedComputeLaunchError(msg, code="provider_unavailable")
        if not selected_clients and not resolved:
            msg = "no compute providers are configured"
            raise ManagedComputeLaunchError(msg, code="provider_unavailable")
        offers: list[ComputeOffer] = []
        for provider_name, client in selected_clients.items():
            try:
                provider_offers = client.list_offers()
            except Exception as exc:
                raise UpstreamUnavailableError(
                    f"compute provider {provider_name!r} offer discovery failed"
                ) from exc
            for offer in provider_offers:
                if offer.provider != provider_name:
                    offer = offer.model_copy(update={"provider": provider_name})
                if _offer_matches_pool(offer, request):
                    offers.append(offer)
        for provider_name, provider in resolved.items():
            client = provider.direct if provider.direct is not None else provider.pooled
            if client is None:
                continue
            try:
                provider_offers = client.list_offers()
            except Exception as exc:
                raise UpstreamUnavailableError(
                    f"compute provider {provider_name!r} offer discovery failed"
                ) from exc
            for offer in provider_offers:
                normalized = (
                    offer
                    if offer.provider == provider_name
                    else offer.model_copy(update={"provider": provider_name})
                )
                if _offer_matches_pool(normalized, request):
                    offers.append(normalized)
        offers.sort(key=lambda item: (_offer_cost(item), -item.reliability, item.provider))
        return offers

    def prepare_pooled_capacity(
        self,
        *,
        workspace: str,
        requirements: ComputeResourceRequirements,
        region: str,
        desired_machines: int,
        workspace_machine_limit: int,
        root_volume_gib: int,
        idle_timeout_seconds: int = 300,
        allowed_instance_types: tuple[str, ...] = (),
    ) -> ComputePoolRecord:
        return self._prepare_pooled_capacity(
            workspace=workspace,
            requirements=requirements,
            region=region,
            desired_machines=desired_machines,
            workspace_machine_limit=workspace_machine_limit,
            root_volume_gib=root_volume_gib,
            idle_timeout_seconds=idle_timeout_seconds,
            allowed_instance_types=allowed_instance_types,
            baseline=None,
        )

    def reconcile_aws_default_capacity(
        self,
        *,
        workspace: str,
        region: str,
        instance_type: str,
        initial_machines: int,
        min_machines: int,
        max_machines: int,
        min_free_cpu_millicores: int,
        min_free_memory_mib: int,
        root_volume_gib: int,
        idle_timeout_seconds: int,
    ) -> ComputePoolRecord:
        """Reconcile the permanent CPU floor for an AWS-default workspace."""

        pool = self._prepare_pooled_capacity(
            workspace=workspace,
            requirements=ComputeResourceRequirements(),
            region=region,
            desired_machines=initial_machines,
            workspace_machine_limit=max_machines,
            root_volume_gib=root_volume_gib,
            idle_timeout_seconds=idle_timeout_seconds,
            allowed_instance_types=(instance_type,),
            baseline=_PooledCapacityBaseline(
                initial_machines=initial_machines,
                min_machines=min_machines,
                min_free_cpu_millicores=min_free_cpu_millicores,
                min_free_memory_mib=min_free_memory_mib,
            ),
        )
        if pool.desired_machines > 0 or pool.observed_machines == 0:
            return pool
        # A lowered floor that reaches zero has to run through the guarded scale
        # owner: it is what releases open capacity operations and retires the
        # sizing state. Writing a zero record alone leaves both behind, and the
        # sizing reconciler raises the machine straight back. This runs after the
        # preparing session has closed; `scale_internal_pool` takes the mutation
        # lease and locks the same row.
        return self.scale_internal_pool(
            pool.workspace_id,
            pool.name,
            0,
            before_mutation=_policy_owned_scale,
        )

    def clear_aws_default_capacity(self, *, workspace: str, release_capacity: bool) -> None:
        """Return internal AWS capacity to demand-owned zero-floor policy.

        ``release_capacity`` is set when the workspace policy allows zero AWS
        machines: every internal pool still holding durable desired capacity is
        then driven to zero through the guarded scale owner. The zero intent is
        persisted before the provider mutation, so a provider failure degrades
        the pool and ``reconcile_pooled_capacity`` converges it later instead of
        leaving billed machines behind.
        """

        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            self._clear_other_internal_pool_floors(
                session,
                workspace_id=workspace_id,
                keep_pool_id=None,
            )
        if not release_capacity:
            return
        with self.context.database.session() as session:
            pools = ComputePoolRepository(session).list_internal(workspace_id=workspace_id)
            protected = {
                pool.id: self._machines_holding_active_work(
                    session,
                    workspace_id=workspace_id,
                    pool_id=pool.id,
                )
                for pool in pools
            }
        for pool in pools:
            if pool.phase in {ComputePoolPhase.Deleting, ComputePoolPhase.Deleted}:
                continue
            floor = protected[pool.id]
            if pool.desired_machines <= floor:
                continue
            self.scale_internal_pool(
                workspace_id,
                pool.name,
                floor,
                before_mutation=_policy_owned_scale,
            )

    def _machines_holding_active_work(
        self,
        session: DatabaseSession,
        *,
        workspace_id: str,
        pool_id: str,
    ) -> int:
        """Count this pool's machines that are running customer work.

        Zeroing the policy must not destroy a running workload. The provider
        scales in by picking its own victim, so the floor has to hold the busy
        machines back here; the scheduler's drain owner already refuses to
        release a machine with active containers and takes them one at a time as
        they go idle. Lowering the policy floor is what unblocks that owner, so
        capacity above this count is released now and the rest converges to zero
        as the work finishes.
        """

        machine_ids = {
            instance.machine_id
            for instance in ComputeProviderInstanceRepository(session).list_for_pool(pool_id)
            if instance.machine_id
        }
        if not machine_ids:
            return 0
        busy = {
            container.machine_id or container.runtime_machine_id
            for container in ContainerRepository(session).list(
                workspace_id=workspace_id,
                statuses=(ContainerStatus.Pending.value, ContainerStatus.Running.value),
            )
            if (container.machine_id or container.runtime_machine_id) in machine_ids
        }
        return len(busy)

    def _prepare_pooled_capacity(
        self,
        *,
        workspace: str,
        requirements: ComputeResourceRequirements,
        region: str,
        desired_machines: int,
        workspace_machine_limit: int,
        root_volume_gib: int,
        idle_timeout_seconds: int,
        allowed_instance_types: tuple[str, ...],
        baseline: _PooledCapacityBaseline | None,
    ) -> ComputePoolRecord:
        if self.provider_resolver is None or self.pool_bootstrap_factory is None:
            raise ManagedComputeLaunchError(
                "workspace pooled compute is not configured",
                code="provider_unavailable",
            )
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
        providers = [
            provider
            for provider in self.provider_resolver.list_providers(workspace_id)
            if provider.capacity_mode is ComputeCapacityMode.Pooled and provider.pooled is not None
        ]
        if not providers:
            raise ManagedComputeLaunchError(
                "workspace has no ready pooled compute provider",
                code="provider_unavailable",
            )
        offers: list[ComputeOffer] = []
        for provider in providers:
            pooled = provider.pooled
            if pooled is None:
                continue
            offers.extend(
                offer
                for offer in pooled.list_offers()
                if offer.region == region
                and (not allowed_instance_types or offer.instance_type in allowed_instance_types)
            )
        try:
            offer = choose_offer(
                offers,
                OfferRequest(
                    regions=[region],
                    min_cpu_millicores=requirements.cpu_millicores,
                    min_memory_mb=requirements.memory_mb,
                    min_storage_mb=root_volume_gib * 1024,
                    architecture=requirements.architecture or "amd64",
                    runtime=requirements.runtime,
                    gpu=requirements.gpu,
                    min_gpu_count=requirements.gpu_count,
                    nodes=max(desired_machines, 1),
                ),
            )
        except ValueError as exc:
            raise ManagedComputeLaunchError(
                "no connected AWS capacity matches the workload requirements",
                code="offer_unavailable",
            ) from exc
        provider = next(item for item in providers if item.ref == offer.provider)
        if provider.connection_id is None or provider.pooled is None:
            raise ManagedComputeLaunchError(
                "pooled compute provider connection is unavailable",
                code="provider_unavailable",
            )
        pool_id, pool_name = internal_pool_identity(
            workspace_id=workspace_id,
            provider_ref=provider.ref,
            region=offer.region,
            capability_key=offer.capability_key,
        )
        created = False
        with self.context.database.session() as session:
            repository = ComputePoolRepository(session)
            scheduler_pools = PoolRepository(session)
            current = repository.get_by_identity(
                workspace_id=workspace_id,
                provider_ref=provider.ref,
                region=offer.region,
                capability_key=offer.capability_key,
                for_update=True,
            )
            current_policy = scheduler_pools.get(pool_name, workspace_id=workspace_id)
            other_desired = sum(
                item.desired_machines
                for item in repository.list_internal(workspace_id=workspace_id)
                if current is None or item.id != current.id
                if _pool_gpu_capacity(item) == (requirements.gpu_count > 0)
            )
            remaining = max(workspace_machine_limit - other_desired, 0)
            requested_machines = max(
                desired_machines,
                baseline.initial_machines if baseline is not None else 0,
                baseline.min_machines if baseline is not None else 0,
            )
            if baseline is None:
                # Demand-driven placement never shrinks a pool it did not size.
                desired = min(
                    max(requested_machines, current.desired_machines if current is not None else 0),
                    remaining,
                )
            else:
                desired = _policy_owned_desired_machines(
                    current_desired=current.desired_machines if current is not None else 0,
                    previous_floor=_previous_policy_floor(current, current_policy),
                    floor=requested_machines,
                    ceiling=remaining,
                )
                if current is not None and desired < current.desired_machines:
                    # Release only down to the machines still running work; the
                    # drain owner takes the rest as they go idle.
                    desired = max(
                        desired,
                        min(
                            self._machines_holding_active_work(
                                session,
                                workspace_id=workspace_id,
                                pool_id=current.id,
                            ),
                            current.desired_machines,
                        ),
                    )
            if requested_machines > 0 and desired < requested_machines:
                raise CapacityLimitReachedError(
                    f"workspace compute limit reached: {requested_machines} machines "
                    f"requested, {desired} available within the workspace limit of "
                    f"{workspace_machine_limit}"
                )
            maximum = max(min(workspace_machine_limit, remaining), desired, 1)
            minimum = (
                baseline.min_machines
                if baseline is not None
                else current.min_machines
                if current is not None
                else 0
            )
            if current is None:
                created = True
                current = repository.upsert(
                    ComputePoolRecord(
                        id=pool_id,
                        capacity_owner_id=pool_id,
                        capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                        capacity_owner_source=CapacityOwnerSource.Provider,
                        workspace_id=workspace_id,
                        name=pool_name,
                        selector=pool_name,
                        status=ComputePoolPhase.Provisioning.value,
                        source="workspace_policy",
                        config={
                            "gpu_count": offer.gpu_count,
                            "idle_timeout_seconds": idle_timeout_seconds,
                            "root_volume_gib": root_volume_gib,
                            "workspace_machine_limit": workspace_machine_limit,
                        },
                        provider_ref=provider.ref,
                        provider_connection_id=provider.connection_id,
                        capacity_mode=ComputeCapacityMode.Pooled,
                        visibility=ComputePoolVisibility.Internal,
                        region=offer.region,
                        offer_id=offer.id,
                        capability_key=offer.capability_key,
                        desired_machines=desired,
                        min_machines=minimum,
                        max_machines=maximum,
                        observed_machines=0,
                        generation=1,
                        phase=ComputePoolPhase.Provisioning,
                    )
                )
            elif (
                desired != current.desired_machines
                or minimum != current.min_machines
                or maximum != current.max_machines
                or idle_timeout_seconds
                != _pool_config_int(current, "idle_timeout_seconds", default=300)
            ):
                current = current.model_copy(
                    update={
                        "desired_machines": desired,
                        "min_machines": minimum,
                        "max_machines": maximum,
                        "config": {
                            **current.config,
                            "idle_timeout_seconds": idle_timeout_seconds,
                            "root_volume_gib": root_volume_gib,
                            "workspace_machine_limit": workspace_machine_limit,
                        },
                    }
                )
                current = repository.upsert(current)
            scheduler_pools.upsert(
                Pool(
                    capacity_owner_id=current.capacity_owner_id,
                    capacity_owner_kind=current.capacity_owner_kind,
                    capacity_owner_source=current.capacity_owner_source,
                    name=current.name,
                    provider=current.provider_ref,
                    initial_workers=(
                        baseline.initial_machines
                        if baseline is not None
                        else current_policy.initial_workers
                        if current_policy is not None
                        else 0
                    ),
                    min_workers=current.min_machines,
                    max_workers=current.max_machines,
                    scaling_enabled=True,
                    default_eligible=False,
                    priority=current_policy.priority if current_policy is not None else 0,
                    min_free_cpu_millicores=(
                        baseline.min_free_cpu_millicores
                        if baseline is not None
                        else current_policy.min_free_cpu_millicores
                        if current_policy is not None
                        else 0
                    ),
                    min_free_memory_mib=(
                        baseline.min_free_memory_mib
                        if baseline is not None
                        else current_policy.min_free_memory_mib
                        if current_policy is not None
                        else 0
                    ),
                    min_free_gpu_count=(
                        current_policy.min_free_gpu_count if current_policy is not None else 0
                    ),
                    worker_cpu_millicores=offer.cpu_millicores,
                    worker_memory_mib=offer.memory_mb,
                    worker_gpu_type=offer.gpu or "",
                    worker_gpu_count=offer.gpu_count,
                    worker_runtimes=(offer.runtime,),
                    worker_preemptible=(
                        str(offer.labels.get("preemptible", "false")).strip().lower() == "true"
                    ),
                    idle_drain_timeout_seconds=idle_timeout_seconds,
                    scale_up_cooldown_seconds=(
                        current_policy.scale_up_cooldown_seconds
                        if current_policy is not None
                        else 5
                    ),
                    scale_down_cooldown_seconds=(
                        current_policy.scale_down_cooldown_seconds
                        if current_policy is not None
                        else 60
                    ),
                    registration_timeout_seconds=(
                        current_policy.registration_timeout_seconds
                        if current_policy is not None
                        else 600
                    ),
                    labels={
                        "capacity_mode": ComputeCapacityMode.Pooled.value,
                        "visibility": ComputePoolVisibility.Internal.value,
                    },
                ),
                workspace_id=workspace_id,
            )
            if baseline is not None:
                self._clear_other_internal_pool_floors(
                    session,
                    workspace_id=workspace_id,
                    keep_pool_id=current.id,
                )
        if self.scheduler_hooks is not None:
            self.scheduler_hooks.register_internal_pool(current, offer)
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputePools,
            change=WorkspaceChangeType.Created if created else WorkspaceChangeType.Updated,
            resource_id=current.id,
        )
        return current

    @staticmethod
    def _clear_other_internal_pool_floors(
        session: DatabaseSession,
        *,
        workspace_id: str,
        keep_pool_id: str | None,
    ) -> None:
        internal_pools = ComputePoolRepository(session)
        scheduler_pools = PoolRepository(session)
        for internal_pool in internal_pools.list_internal(workspace_id=workspace_id):
            if internal_pool.id == keep_pool_id:
                continue
            if internal_pool.min_machines:
                internal_pools.upsert(internal_pool.model_copy(update={"min_machines": 0}))
            policy = scheduler_pools.get(
                internal_pool.name,
                workspace_id=workspace_id,
            )
            if policy is None or not any(
                (
                    policy.initial_workers,
                    policy.min_workers,
                    policy.min_free_cpu_millicores,
                    policy.min_free_memory_mib,
                    policy.min_free_gpu_count,
                )
            ):
                continue
            scheduler_pools.upsert(
                policy.model_copy(
                    update={
                        "initial_workers": 0,
                        "min_workers": 0,
                        "min_free_cpu_millicores": 0,
                        "min_free_memory_mib": 0,
                        "min_free_gpu_count": 0,
                    }
                ),
                workspace_id=workspace_id,
            )

    def _required_capacity_owner_mutations(self) -> CapacityOwnerMutationLease:
        if self.capacity_owner_mutations is None:
            raise UpstreamUnavailableError(
                "capacity-owner mutation lease service is not configured"
            )
        return self.capacity_owner_mutations

    def get_internal_pool(
        self,
        workspace_id: str,
        pool_name: str,
    ) -> ComputePoolRecord:
        """Read the durable pooled-provider intent for a workspace-owned pool."""

        with self.context.database.session() as session:
            pool = ComputePoolRepository(session).get_by_name(workspace_id, pool_name)
        return _require_internal_pooled_pool(pool, pool_name=pool_name)

    def scale_internal_pool(
        self,
        workspace_id: str,
        pool_name: str,
        desired_machines: int,
        *,
        before_mutation: Callable[[ComputePoolRecord], None],
        now: datetime | None = None,
    ) -> ComputePoolRecord:
        """Serialize, guard, persist, and apply one durable provider capacity intent."""

        if desired_machines < 0:
            raise InvalidInputError("desired compute pool capacity cannot be negative")
        initial = self.get_internal_pool(workspace_id, pool_name)
        mutations = self._required_capacity_owner_mutations()
        try:
            with mutations.mutation_lock(initial.capacity_owner_id):
                return self._scale_internal_pool_under_lease(
                    workspace_id,
                    pool_name,
                    desired_machines,
                    capacity_owner_id=initial.capacity_owner_id,
                    before_mutation=before_mutation,
                    now=now,
                )
        except DomainError:
            raise
        except Exception as exc:
            raise UpstreamUnavailableError(
                "compute capacity-owner mutation lease is unavailable"
            ) from exc

    def _scale_internal_pool_under_lease(
        self,
        workspace_id: str,
        pool_name: str,
        desired_machines: int,
        *,
        capacity_owner_id: str,
        before_mutation: Callable[[ComputePoolRecord], None],
        now: datetime | None,
    ) -> ComputePoolRecord:
        current_time = _utc(now)
        verify_provider_zero = False
        with self.context.database.session() as session:
            pools = ComputePoolRepository(session)
            pool = _require_internal_pooled_pool(
                pools.get_by_name(workspace_id, pool_name, for_update=True),
                pool_name=pool_name,
            )
            if pool.capacity_owner_id != capacity_owner_id:
                raise ConflictError(f"compute pool {pool_name!r} capacity owner changed")
            before_mutation(pool)
            if pool.provider_state.degraded_reason is not None:
                # An explicit capacity mutation supersedes the durable degraded
                # reason and re-enables capacity restoration.
                pool = pool.model_copy(
                    update={
                        "provider_state": pool.provider_state.model_copy(
                            update={"degraded_reason": None}
                        )
                    }
                )
            stored_workspace_limit = _pool_config_int(
                pool,
                "workspace_machine_limit",
                default=pool.max_machines,
            )
            policy = WorkspaceComputePolicyRepository(session).get_for_workspace(workspace_id)
            if policy is None:
                workspace_limit = stored_workspace_limit
            elif _pool_gpu_capacity(pool):
                workspace_limit = policy.aws.max_gpu_instances
            else:
                workspace_limit = policy.aws.max_cpu_instances
            other_desired = sum(
                item.desired_machines
                for item in pools.list_internal(workspace_id=workspace_id)
                if item.id != pool.id and _pool_gpu_capacity(item) == _pool_gpu_capacity(pool)
            )
            available = max(workspace_limit - other_desired, 0)
            if desired_machines < pool.min_machines:
                raise InvalidInputError(
                    f"compute pool {pool_name!r} requires at least {pool.min_machines} machines"
                )
            if desired_machines > available:
                raise ConflictError(
                    f"workspace pooled compute capacity limit is {available} machines"
                )
            maximum = max(available, desired_machines, 1)
            sizing_states = PoolRepository(session)
            sizing_state = (
                sizing_states.get_sizing_state(pool.capacity_owner_id, for_update=True)
                if desired_machines == 0
                else None
            )
            if desired_machines == 0 and sizing_state is None:
                raise ConflictError(
                    f"compute pool sizing state does not exist: {pool.capacity_owner_id}"
                )
            if desired_machines == 0:
                operations = ComputeCapacityOperationRepository(session)
                for operation in operations.list_open_for_owner(pool.capacity_owner_id):
                    operations.upsert(
                        operation.model_copy(
                            update={
                                "status": "released",
                                "release_desired_unit": 0,
                                "last_error": "",
                                "updated_at": current_time,
                            }
                        )
                    )
            if (
                desired_machines == 0
                and sizing_state is not None
                and _zero_capacity_converged(pool, sizing_state)
            ):
                verify_provider_zero = True
                intent = pool
            else:
                intent = pools.update_capacity(
                    pool.id,
                    expected_generation=pool.generation,
                    desired_machines=desired_machines,
                    max_machines=maximum,
                    observed_machines=pool.observed_machines,
                    phase=ComputePoolPhase.Updating,
                    provider_state=pool.provider_state,
                )
                if intent is None:
                    raise ConflictError(
                        f"compute pool {pool_name!r} capacity intent was superseded"
                    )
                if sizing_state is not None:
                    retired = sizing_states.compare_and_set_sizing_state(
                        _zero_sizing_state_update(sizing_state, now=current_time)
                    )
                    if retired is None:
                        raise ConflictError(
                            f"compute pool {pool_name!r} sizing intent was superseded"
                        )

        try:
            provider, offer = self._resolved_internal_pool_provider(intent)
            if provider.pooled is None:
                raise UpstreamUnavailableError(f"compute pool {pool_name!r} provider is not pooled")
            if verify_provider_zero:
                observed = provider.pooled.describe_pool(self._provider_pool_request(intent, offer))
                if _provider_zero_capacity_converged(observed):
                    return self.provider_machines._apply_pooled_snapshot(
                        intent,
                        offer,
                        observed,
                        provider=provider.pooled,
                        update_capacity=False,
                        now=current_time,
                    )
                intent = self.provider_machines._persist_zero_capacity_repair(
                    intent,
                    maximum=maximum,
                    observed=observed,
                    now=current_time,
                )
            snapshot = provider.pooled.set_pool_capacity(
                # Creates the autoscaling group, and its launch template with
                # it, when the pool has none yet.
                self._provider_pool_request(intent, offer),
                desired_machines=intent.desired_machines,
                max_machines=intent.max_machines,
            )
            return self.provider_machines._apply_pooled_snapshot(
                intent,
                offer,
                snapshot,
                provider=provider.pooled,
                update_capacity=False,
                now=current_time,
            )
        except ConflictError:
            raise
        except Exception as exc:
            self._mark_pooled_capacity_degraded(intent)
            if isinstance(exc, (InvalidInputError, UpstreamUnavailableError)):
                raise
            raise UpstreamUnavailableError(
                f"compute pool {pool_name!r} provider capacity update failed"
            ) from exc

    def describe_internal_pool(
        self,
        workspace_id: str,
        pool_name: str,
    ) -> tuple[ComputePoolRecord, ProviderPoolSnapshot]:
        pool, provider, offer = self._internal_pool_provider(workspace_id, pool_name)
        if provider.pooled is None:
            raise RuntimeError("internal compute pool does not use pooled capacity")
        snapshot = provider.pooled.describe_pool(self._provider_pool_request(pool, offer))
        updated = self.provider_machines._apply_pooled_snapshot(
            pool,
            offer,
            snapshot,
            provider=provider.pooled,
            update_capacity=False,
        )
        return updated, snapshot

    def release_internal_pool_machine(
        self,
        workspace_id: str,
        pool_name: str,
        machine_id: str,
    ) -> ComputePoolRecord:
        pool, provider, offer = self._internal_pool_provider(workspace_id, pool_name)
        if provider.pooled is None:
            raise RuntimeError("internal compute pool does not use pooled capacity")
        with self.context.database.session() as session:
            record = next(
                (
                    item
                    for item in ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
                    if item.machine_id == machine_id and item.instance_id is not None
                ),
                None,
            )
        if record is None or record.instance_id is None:
            raise KeyError(f"provider instance for machine not found: {machine_id}")
        snapshot = provider.pooled.release_machine(
            self._provider_pool_request(pool, offer),
            record.instance_id,
        )
        if self.scheduler_hooks is not None:
            self.scheduler_hooks.disable_machine(machine_id, "idle_pool_scale_down")
        target = max(pool.desired_machines - 1, pool.min_machines)
        return self.provider_machines._apply_pooled_snapshot(
            pool.model_copy(update={"desired_machines": target}),
            offer,
            snapshot,
            provider=provider.pooled,
            update_capacity=True,
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
                ComputePoolRepository(session).get(instance.pool_id)
                if instance is not None and instance.pool_id is not None
                else None
            )
        if pool is None:
            return False
        if (
            pool.workspace_id != workspace_id
            or pool.visibility is not ComputePoolVisibility.Internal
            or pool.capacity_mode is not ComputeCapacityMode.Pooled
        ):
            raise UpstreamUnavailableError("provider machine ownership is inconsistent")
        self.release_internal_pool_machine(workspace_id, pool.name, machine_id)
        return True

    def reconcile_pooled_capacity(
        self,
        *,
        now: datetime | None = None,
    ) -> list[ComputePoolRecord]:
        current_time = _utc(now)
        mutations = self._required_capacity_owner_mutations()
        with self.context.database.session() as session:
            pools = ComputePoolRepository(session).list_internal_across_workspaces()
        reconciled: list[ComputePoolRecord] = []
        for pool in pools:
            try:
                with mutations.mutation_lock(pool.capacity_owner_id):
                    current = self._reconcile_pooled_pool(
                        pool.id,
                        now=current_time,
                    )
            except ConflictError:
                continue
            if current is not None:
                reconciled.append(current)
        return reconciled

    def _reconcile_pooled_pool(
        self,
        pool_id: str,
        *,
        now: datetime,
    ) -> ComputePoolRecord | None:
        with self.context.database.session() as session:
            current = ComputePoolRepository(session).get(pool_id)
        if current is None:
            return None
        if current.phase is ComputePoolPhase.Deleted:
            self._retire_proven_provider_pool_machines(current, now=now)
            return None
        try:
            provider, offer = self._resolved_internal_pool_provider(current)
            pooled = provider.pooled
            if pooled is None:
                raise UpstreamUnavailableError(
                    f"compute pool {current.name!r} provider is not pooled"
                )
            if current.phase is ComputePoolPhase.Deleting:
                snapshot = pooled.delete_pool(self._provider_pool_request(current, offer))
                return self.provider_machines._apply_pooled_snapshot(
                    current,
                    offer,
                    snapshot,
                    provider=pooled,
                    update_capacity=False,
                    now=now,
                )
            current = self._reclaim_pooled_bootstrap_failures(
                current,
                pooled=pooled,
                offer=offer,
                now=now,
            )
            degraded = current.provider_state.degraded_reason is not None
            request = self._provider_pool_request(current, offer)
            snapshot = (
                # A durably degraded pool stopped relaunching: observe and prove
                # terminations without restoring provider capacity until an
                # explicit capacity mutation clears the degraded reason.
                pooled.describe_pool(request) if degraded else pooled.ensure_pool(request)
            )
            return self.provider_machines._apply_pooled_snapshot(
                current,
                offer,
                snapshot,
                provider=pooled,
                update_capacity=False,
                now=now,
            )
        except Exception:
            LOGGER.exception(
                "pooled provider reconciliation failed for pool %s (%s)",
                current.name,
                current.id,
            )
            return self._mark_pooled_capacity_degraded(
                current,
                preserve_deleting=True,
            )

    def _reclaim_pooled_bootstrap_failures(
        self,
        pool: ComputePoolRecord,
        *,
        pooled: PooledCapacityProvider,
        offer: ComputeOffer,
        now: datetime,
    ) -> ComputePoolRecord:
        """Reclaim pooled machines that missed their bootstrap phase deadline.

        Terminal proof stays with the pooled snapshot path: a reclaimed record
        is marked terminating here and released at the provider, and it becomes
        deleted only once ``machine_storage_destroyed`` proves the instance and
        its volumes absent during snapshot application. Exhausted relaunch
        attempts durably degrade the pool instead of relaunching forever.
        """
        to_reclaim: list[tuple[ComputeProviderInstanceRecord, MachineBootstrapFailureReason]] = []
        with self.context.database.session() as session:
            for record in ComputeProviderInstanceRepository(session).list_for_pool(pool.id):
                if not _reservation_open(record.status):
                    continue
                if record.status == ReservationStatus.Terminating.value:
                    continue
                failure = self.provider_machines._provider_bootstrap_failure_to_reclaim(
                    session,
                    pool,
                    record,
                    now=now,
                )
                if failure is not None:
                    to_reclaim.append((record, failure))
        if not to_reclaim:
            return pool
        current = pool
        attempts_exhausted = False
        for record, failure in to_reclaim:
            with self.context.database.session() as session:
                self.provider_machines._terminate_provider_record(
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
            streak = record.launch_attempt - current.provider_state.launch_attempt_baseline
            if streak >= self.reclaim.max_launch_attempts_for(record.provider):
                attempts_exhausted = True
            if record.instance_id is not None:
                snapshot = pooled.release_machine(
                    self._provider_pool_request(current, offer),
                    record.instance_id,
                )
                current = self.provider_machines._apply_pooled_snapshot(
                    current,
                    offer,
                    snapshot,
                    provider=pooled,
                    update_capacity=False,
                    now=now,
                )
            LOGGER.warning(
                "reclaimed pooled provider machine that did not become ready",
                extra={
                    "provider": record.provider,
                    "pool_name": current.name,
                    "machine_id": record.machine_id,
                    "provider_instance_id": record.instance_id or record.id,
                    "launch_attempt": record.launch_attempt,
                },
            )
        if attempts_exhausted:
            degraded = self._mark_pooled_capacity_degraded(
                current,
                reason="bootstrap_launch_attempts_exhausted",
            )
            if degraded is not None:
                current = degraded
        return current

    def request_connection_drain(
        self,
        connection_id: str,
        *,
        workspace: str,
    ) -> AwsAccountPoolDrain:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            pools = ComputePoolRepository(session).list_for_provider_connection(connection_id)
        if any(pool.workspace_id != workspace_id for pool in pools):
            raise UpstreamUnavailableError("AWS capacity ownership is inconsistent")
        with self.context.database.session() as session:
            self._clear_other_internal_pool_floors(
                session,
                workspace_id=workspace_id,
                keep_pool_id=None,
            )

        for pool in pools:
            if pool.phase is ComputePoolPhase.Deleted:
                self._retire_proven_provider_pool_machines(pool, now=utc_now())
                continue
            current = pool
            if pool.phase is not ComputePoolPhase.Deleting:
                with self.context.database.session() as session:
                    current = ComputePoolRepository(session).update_capacity(
                        pool.id,
                        expected_generation=pool.generation,
                        desired_machines=0,
                        max_machines=max(pool.max_machines, 1),
                        observed_machines=pool.observed_machines,
                        phase=ComputePoolPhase.Deleting,
                        provider_state=pool.provider_state,
                    )
                if current is None:
                    raise UpstreamUnavailableError("AWS capacity drain was superseded")
            try:
                durable, provider, offer = self._internal_pool_provider(
                    workspace_id,
                    current.name,
                )
                if provider.pooled is None:
                    raise RuntimeError("AWS capacity provider is not pooled")
                snapshot = provider.pooled.delete_pool(self._provider_pool_request(durable, offer))
                self.provider_machines._apply_pooled_snapshot(
                    durable,
                    offer,
                    snapshot,
                    provider=provider.pooled,
                    update_capacity=False,
                )
            except Exception as exc:
                raise UpstreamUnavailableError(
                    f"AWS capacity {current.name!r} could not be drained"
                ) from exc

        with self.context.database.session() as session:
            remaining = sum(
                pool.phase is not ComputePoolPhase.Deleted
                for pool in ComputePoolRepository(session).list_for_provider_connection(
                    connection_id
                )
            )
        return AwsAccountPoolDrain(total_pools=len(pools), remaining_pools=remaining)

    def launch_pool_capacity(
        self,
        config: PoolConfig,
        *,
        workspace: str = "default",
        nodes: int = 0,
        owner_token_id: str = NAME,
        now: datetime | None = None,
    ) -> PrivatePoolState:
        try:
            return self._launch_pool_capacity(
                config,
                workspace=workspace,
                nodes=nodes,
                owner_token_id=owner_token_id,
                now=now,
            )
        except ManagedComputeLaunchError as exc:
            raise _launch_failure_as_domain_error(exc) from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def _launch_pool_capacity(
        self,
        config: PoolConfig,
        *,
        workspace: str = "default",
        nodes: int = 0,
        owner_token_id: str = NAME,
        now: datetime | None = None,
    ) -> PrivatePoolState:
        current_time = _utc(now)
        plan = compute_pool_from_config(config, node_count=nodes, require_reservation=True)
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
        provider_snapshot = self._provider_client_snapshot(workspace_id)
        clients = self._provider_clients_for(plan.providers, provider_snapshot)
        if not clients:
            msg = "no compute providers are configured"
            raise ManagedComputeLaunchError(msg, code="provider_unavailable")

        offer_request = compute_pool_from_config(
            config.model_copy(update={"nodes": plan.nodes, "gpu": plan.gpu}),
            node_count=plan.nodes,
            require_reservation=False,
        )
        offers = self._list_pool_offers(
            offer_request,
            workspace_id=workspace_id,
            clients=provider_snapshot,
        )
        if not offers:
            msg = "no compatible provider offers are available"
            raise ManagedComputeLaunchError(msg, code="no_compatible_capacity")

        prepared_launches: list[PreparedProviderLaunch] = []
        with self.context.database.session() as session:
            pool_repository = PoolRepository(session)
            compute_pool = pool_repository.get(plan.name, workspace_id=workspace_id)
            selected_offer = offers[0]
            owner_kind, owner_source = (
                (
                    CapacityOwnerKind.PooledProvider,
                    CapacityOwnerSource.Provider,
                )
                if selected_offer.capacity_mode is ComputeCapacityMode.Pooled
                else (
                    CapacityOwnerKind.ManagedPool,
                    CapacityOwnerSource.Managed,
                )
            )
            if compute_pool is None:
                compute_pool = pool_repository.upsert(
                    Pool(
                        name=plan.name,
                        provider=plan.providers[0] if plan.providers else selected_offer.provider,
                        capacity_owner_kind=owner_kind,
                        capacity_owner_source=owner_source,
                        initial_workers=plan.nodes,
                        min_workers=0,
                        max_workers=max(
                            plan.nodes,
                            selected_offer.available * max(selected_offer.node_count, 1),
                            1,
                        ),
                        scaling_enabled=True,
                        worker_cpu_millicores=selected_offer.cpu_millicores,
                        worker_memory_mib=selected_offer.memory_mb,
                        worker_gpu_type=selected_offer.gpu or "",
                        worker_gpu_count=selected_offer.gpu_count,
                        worker_runtimes=(selected_offer.runtime,),
                        labels=_pool_labels_from_config(config),
                    ),
                    workspace_id=workspace_id,
                )
            elif (
                compute_pool.capacity_owner_kind is not owner_kind
                or compute_pool.capacity_owner_source is not owner_source
            ):
                raise ConflictError(
                    f"compute pool {plan.name!r} is owned by "
                    f"{compute_pool.capacity_owner_kind.value!r} capacity"
                )
            elif plan.nodes > compute_pool.max_workers:
                raise CapacityLimitReachedError(
                    f"compute pool {plan.name!r} limit reached: {plan.nodes} workers "
                    f"requested, maximum is {compute_pool.max_workers}"
                )
            offers = [
                offer for offer in offers if _offer_matches_capacity_policy(offer, compute_pool)
            ]
            if not offers:
                raise ConflictError(
                    f"compute pool {plan.name!r} has no offers matching its fixed worker shape"
                )
            pool_worker_limit = compute_pool.max_workers
            compute_pool_repository = ComputePoolRepository(session)
            compute_pool = self._ensure_compute_pool_record(
                compute_pool_repository,
                workspace_id=workspace_id,
                compute_pool=compute_pool,
                config=config,
                source=ComputePoolSource.Managed.value,
                now=current_time,
            )
            locked_pool = compute_pool_repository.get(compute_pool.id, for_update=True)
            if locked_pool is None:
                raise RuntimeError("compute pool disappeared during capacity launch")
            compute_pool = locked_pool
            existing_state = self._private_pool_state_from_records(
                compute_pool,
                ComputeProviderInstanceRepository(session).list_for_pool(compute_pool.id),
            )
            validate_pool_resource_compatibility(existing_state, plan)

            capacity_requests = ComputeCapacityRequestRepository(session)
            active_capacity = capacity_requests.active_for_pool(
                compute_pool.id,
                for_update=True,
            )
            requested_deadline = current_time + timedelta(seconds=plan.ttl_seconds)
            aggregate_deadline = max(
                requested_deadline,
                active_capacity.expires_at
                if active_capacity is not None and active_capacity.expires_at is not None
                else requested_deadline,
            )
            aggregate_budget = plan.max_spend_micros + (
                active_capacity.max_spend_micros if active_capacity is not None else 0
            )
            aggregate_ttl_seconds = max(
                int((aggregate_deadline - current_time).total_seconds()),
                1,
            )
            aggregate_nodes = existing_state.reserved_nodes + plan.nodes
            if aggregate_nodes > pool_worker_limit:
                raise ConflictError(
                    f"compute pool {plan.name!r} request would exceed its maximum of "
                    f"{pool_worker_limit} workers"
                )

            demand = ComputeDemand(
                pool_name=plan.name,
                selector=plan.selector,
                gpu=plan.gpu,
                nodes=aggregate_nodes,
                offer_id=plan.offer_id,
                ttl_seconds=aggregate_ttl_seconds,
                max_spend_micros=aggregate_budget,
                providers=plan.providers,
                regions=plan.regions,
                min_reliability=plan.min_reliability,
            )
            solve = solve_compute_capacity(
                demand,
                offers,
                [
                    _compute_reservation_from_record(item, pool_name=plan.name)
                    for item in existing_state.reservations
                ],
                now_seconds=_unix_seconds(current_time),
            )
            solver_run = self._record_solver_run(
                session,
                workspace_id=workspace_id,
                pool_id=compute_pool.id,
                solve=solve,
            )
            if not solve.feasible:
                raise ManagedComputeLaunchError(
                    solve.reason or "no compatible provider capacity",
                    code="no_compatible_capacity",
                )
            self._check_launch_credit(workspace_id, plan.name, solve)

            capacity_request = capacity_requests.upsert(
                active_capacity.model_copy(
                    update={
                        "max_spend_micros": aggregate_budget,
                        "ttl_seconds": aggregate_ttl_seconds,
                        "expires_at": aggregate_deadline,
                    }
                )
                if active_capacity is not None
                else ComputeCapacityRequestRecord(
                    id=str(uuid4()),
                    workspace_id=workspace_id,
                    pool_id=compute_pool.id,
                    source=ComputePoolSource.Managed.value,
                    max_spend_micros=aggregate_budget,
                    ttl_seconds=aggregate_ttl_seconds,
                    status="active",
                    expires_at=aggregate_deadline,
                    metadata=_json_object(
                        _CapacityRequestMetadata(
                            selector=plan.selector,
                            providers=plan.providers,
                            regions=plan.regions,
                            gpu=plan.gpu,
                        )
                    ),
                )
            )

            for action in solve.actions:
                self._record_solver_decision(session, solver_run, action)
                if action.action is not SolveActionType.Create or action.offer is None:
                    continue
                if action.offer.provider not in clients:
                    msg = f"provider {action.offer.provider!r} is not configured"
                    raise ManagedComputeLaunchError(msg, code="provider_unavailable")
                for _ in range(action.count):
                    machine = self._create_provider_machine(
                        session,
                        workspace_id=workspace_id,
                        pool_name=plan.name,
                        provider_name=action.offer.provider,
                        offer=action.offer,
                    )
                    join_token = plan_join_token_creation(
                        ComputePrincipal(
                            workspace_id=workspace_id,
                            owner_token_id=owner_token_id,
                        ),
                        plan.name,
                        machine_id=machine.id,
                        now=current_time,
                    )
                    ComputeJoinCredentialRepository(session).create(
                        token_hash=join_token.token_hash,
                        workspace_id=workspace_id,
                        pool_name=plan.name,
                        machine_id=machine.id,
                        created_by_token_id=try_uuid(owner_token_id),
                        max_uses=join_token.state.max_uses,
                        expires_at=join_token.expires_at,
                    )
                    launch_intent = self.provider_machines._record_provider_instance(
                        session,
                        pool_id=compute_pool.id,
                        capacity_request_id=capacity_request.id,
                        machine=machine,
                        offer=action.offer,
                        remote_id=f"intent:{machine.id}",
                        status=ProviderMachineStatus.Pending,
                        source=ComputePoolSource.Managed.value,
                        ttl_seconds=aggregate_ttl_seconds,
                        now=current_time,
                        registration_token_hash=join_token.token_hash,
                        launch_state=_LAUNCH_STATE_INTENT,
                    )
                    prepared_launches.append(
                        PreparedProviderLaunch(
                            provider=action.offer.provider,
                            offer=action.offer,
                            machine=machine,
                            provider_record_id=launch_intent.id,
                            registration_token=join_token.token,
                        )
                    )

            provider_instances = ComputeProviderInstanceRepository(session)
            for instance in provider_instances.list_for_pool(compute_pool.id):
                if not _reservation_open(instance.status):
                    continue
                aligned = _provider_record_at_deadline(instance, aggregate_deadline)
                if aligned != instance:
                    provider_instances.upsert(aligned)
            aggregate_config = config.model_copy(
                update={
                    "nodes": aggregate_nodes,
                    "ttl": f"{aggregate_ttl_seconds}s",
                    "max_spend": aggregate_budget / 1_000_000,
                }
            )
            compute_pool = compute_pool_repository.upsert(
                compute_pool.model_copy(
                    update={
                        "config": _json_object(aggregate_config),
                        "expires_at": aggregate_deadline,
                    }
                )
            )

        created_instances: list[LaunchedProviderInstance] = []
        attempted_machine_ids: set[str] = set()
        registered_machines: list[Machine] = []
        try:
            for prepared in prepared_launches:
                provider = clients[prepared.provider]
                attempted_machine_ids.add(prepared.machine.id)
                remote = provider.launch_machine(
                    DirectMachineLaunchRequest(
                        workspace_id=workspace_id,
                        pool_name=plan.name,
                        registration_token=prepared.registration_token,
                        offer=prepared.offer,
                        machine_id=prepared.machine.id,
                        operation_id=prepared.machine.id,
                        idempotency_key=prepared.machine.id,
                    )
                )
                created_instances.append(
                    LaunchedProviderInstance(
                        provider=prepared.provider,
                        provider_instance_id=remote.provider_instance_id,
                        machine_id=prepared.machine.id,
                        status=remote.status,
                        address=remote.address,
                        storage_volume_ids=remote.storage_volume_ids,
                    )
                )

            created_by_machine = {item.machine_id: item for item in created_instances}
            with self.context.database.session() as session:
                compute_pool_repository = ComputePoolRepository(session)
                compute_pool = compute_pool_repository.get(compute_pool.id, for_update=True)
                if compute_pool is None:
                    raise RuntimeError("compute pool disappeared after provider launch")
                provider_instances = ComputeProviderInstanceRepository(session)
                machines = MachineRepository(session)
                for prepared in prepared_launches:
                    remote = created_by_machine[prepared.machine.id]
                    machine = machines.get_across_workspaces(prepared.machine.id)
                    if machine is None:
                        raise RuntimeError("provider launch machine intent disappeared")
                    machine = self._update_machine_after_launch(
                        session,
                        machine,
                        provider_instance_id=remote.provider_instance_id,
                        status=remote.status,
                        address=remote.address,
                    )
                    registered_machines.append(machine)
                    record = provider_instances.records.get(prepared.provider_record_id)
                    if record is None:
                        raise RuntimeError("provider launch intent disappeared")
                    provider_instances.upsert(
                        record.model_copy(
                            update={
                                "instance_id": remote.provider_instance_id,
                                "status": _reservation_status_from_provider(remote.status).value,
                                "bootstrap_phase": MachineBootstrapPhase.Provisioning,
                                "bootstrap_failure_reason": None,
                                "bootstrap_observed_at": utc_now(),
                                "metadata": {
                                    **_provider_instance_metadata(record),
                                    "launch_state": _LAUNCH_STATE_COMMITTED,
                                    "storage_volume_ids": list(remote.storage_volume_ids),
                                },
                                "updated_at": utc_now(),
                            }
                        )
                    )
                state = self._private_pool_state_from_records(
                    compute_pool,
                    provider_instances.list_for_pool(compute_pool.id),
                )
        except Exception as exc:
            terminated_machine_ids = self._compensate_failed_launch(
                created_instances,
                clients=clients,
            )
            try:
                self.provider_machines._record_failed_launch_cleanup(
                    prepared_launches,
                    created_instances=created_instances,
                    attempted_machine_ids=attempted_machine_ids,
                    terminated_machine_ids=terminated_machine_ids,
                    failure=f"provider launch failed ({type(exc).__name__})",
                )
            except Exception:
                LOGGER.exception(
                    "failed to persist provider launch compensation; durable intents remain",
                    extra={"pool_name": plan.name},
                )
            raise

        if self.scheduler_hooks is not None:
            for machine in registered_machines:
                self.scheduler_hooks.register_machine(machine)
            self.scheduler_hooks.register_pool(state)
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputePools,
            change=WorkspaceChangeType.Updated,
            resource_id=plan.name,
        )
        for machine_id in created_by_machine:
            self._publish_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputeMachines,
                change=WorkspaceChangeType.Created,
                resource_id=machine_id,
            )
        return state

    def extend_pool_capacity(
        self,
        name: str,
        *,
        workspace: str = "default",
        ttl: str,
        max_spend: float,
        now: datetime | None = None,
    ) -> PrivatePoolState:
        try:
            return self._extend_pool_capacity(
                name,
                workspace=workspace,
                ttl=ttl,
                max_spend=max_spend,
                now=now,
            )
        except KeyError as exc:
            raise NotFoundError(str(exc).strip("'\"")) from exc
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc

    def _extend_pool_capacity(
        self,
        name: str,
        *,
        workspace: str = "default",
        ttl: str,
        max_spend: float,
        now: datetime | None = None,
    ) -> PrivatePoolState:
        current_time = _utc(now)
        ttl_seconds = parse_ttl_seconds(ttl)
        if ttl_seconds <= 0:
            raise ValueError("pool capacity extension requires ttl")
        requested_budget = dollars_to_micros(max_spend)
        if requested_budget <= 0:
            raise ValueError("pool capacity extension requires max_spend")
        requested_deadline = current_time + timedelta(seconds=ttl_seconds)
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            pools = ComputePoolRepository(session)
            pool = pools.get_by_name(workspace_id, name)
            if pool is None:
                raise KeyError(f"compute pool not found: {name}")
            pool = pools.get(pool.id, for_update=True)
            if pool is None:
                raise RuntimeError("compute pool disappeared during capacity extension")
            current = ComputeCapacityRequestRepository(session).active_for_pool(
                pool.id,
                for_update=True,
            )
            if current is None or current.expires_at is None:
                raise ValueError("pool has no active managed capacity")
            if requested_deadline <= current.expires_at:
                raise ValueError("pool capacity deadline may only be extended")
            instances = ComputeProviderInstanceRepository(session)
            records = instances.list_for_pool(pool.id)
            projected_records = [
                _provider_record_at_deadline(record, requested_deadline)
                for record in records
                if _reservation_open(record.status)
            ]
            projected_commitment = sum(
                record.committed_micros
                for record in projected_records
                if record.source == ComputePoolSource.Managed.value
            )
            current_spend = _recorded_pool_spend_micros(
                session,
                workspace_id=workspace_id,
                pool_id=pool.id,
            )
            minimum_budget = max(
                current.max_spend_micros,
                projected_commitment,
                current_spend,
            )
            if requested_budget < minimum_budget:
                raise ValueError(
                    "max_spend must be no lower than the current capacity budget "
                    "and projected commitment or recorded spend"
                )
            capacity = current.model_copy(
                update={
                    "max_spend_micros": requested_budget,
                    "ttl_seconds": ttl_seconds,
                    "expires_at": requested_deadline,
                }
            )
            ComputeCapacityRequestRepository(session).upsert(capacity)
            for record in projected_records:
                instances.upsert(record)
            config = (
                PoolConfig.model_validate(pool.config) if pool.config else PoolConfig(name=name)
            )
            pool = pools.upsert(
                pool.model_copy(
                    update={
                        "config": _json_object(
                            config.model_copy(
                                update={
                                    "ttl": f"{ttl_seconds}s",
                                    "max_spend": requested_budget / 1_000_000,
                                }
                            )
                        ),
                        "expires_at": requested_deadline,
                    }
                )
            )
            state = self._private_pool_state_from_records(
                pool,
                instances.list_for_pool(pool.id),
            )
            if self.scheduler_hooks is not None:
                self.scheduler_hooks.register_pool(state)
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputePools,
            change=WorkspaceChangeType.Updated,
            resource_id=name,
        )
        return state

    def get_private_pool_state(self, name: str) -> PrivatePoolState | None:
        workspace_id = self._workspace_id()
        with self.context.database.session() as session:
            pool = ComputePoolRepository(session).get_by_name(workspace_id, name)
            if pool is None:
                return None
            instances = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        return self._private_pool_state_from_records(pool, instances)

    def terminate_pool_machine(
        self,
        pool_name: str,
        machine_id: str,
        *,
        reason: str = "idle_pool_scale_down",
        message: str = "managed compute machine idle past drain threshold",
    ) -> PrivatePoolState:
        workspace_id = self._workspace_id()
        clients = self._provider_client_snapshot(workspace_id)
        with self.context.database.session() as session:
            compute_pool = ComputePoolRepository(session).get_by_name(workspace_id, pool_name)
            if compute_pool is None:
                msg = f"compute pool not found: {pool_name}"
                raise KeyError(msg)
            instances = ComputeProviderInstanceRepository(session).list_for_pool(compute_pool.id)
            record = next(
                (
                    item
                    for item in instances
                    if item.machine_id == machine_id and _reservation_open(item.status)
                ),
                None,
            )
            if record is None:
                msg = f"active provider machine not found: {machine_id}"
                raise KeyError(msg)
            changed = self.provider_machines._terminate_provider_record(
                session,
                record,
                clients=clients,
                reason=reason,
                message=message,
            )
            refreshed = ComputeProviderInstanceRepository(session).list_for_pool(compute_pool.id)
            state = self._private_pool_state_from_records(compute_pool, refreshed)
            if changed and self.scheduler_hooks is not None:
                self.scheduler_hooks.register_pool(state)
        if changed:
            self._publish_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputePools,
                change=WorkspaceChangeType.Updated,
                resource_id=pool_name,
            )
            self._publish_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputeMachines,
                change=WorkspaceChangeType.Deleted,
                resource_id=machine_id,
            )
        return state

    def create_machine(
        self,
        *,
        workspace: str = "default",
        pool: str = "default",
        provider: str = "local",
        cpu: float | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        address: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Machine:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            machine = MachineRepository(session).records.create(
                {
                    "pool": pool,
                    "provider": provider,
                    "cpu": cpu,
                    "memory": memory,
                    "gpu": gpu,
                    "address": address,
                    "labels": labels or {},
                    "status": ResourceStatus.Created.value,
                },
                workspace_id=workspace_id,
                status=ResourceStatus.Created.value,
            )
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeMachines,
            change=WorkspaceChangeType.Created,
            resource_id=machine.id,
        )
        return machine

    def list_machines(self, *, workspace: str = "default") -> list[Machine]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = [
                machine
                for machine in MachineRepository(session).records.list(workspace_id=workspace_id)
                if machine.status is not ResourceStatus.Deleted
            ]
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def delete_machine(self, machine_id: str, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            scoped_ids = {
                machine.id
                for machine in MachineRepository(session).records.list(workspace_id=workspace_id)
            }
            if machine_id not in scoped_ids:
                msg = f"machine not found in workspace: {machine_id}"
                raise KeyError(msg)
            MachineRepository(session).records.delete(machine_id, workspace_id=workspace_id)
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeMachines,
            change=WorkspaceChangeType.Deleted,
            resource_id=machine_id,
        )

    def register_worker(
        self,
        *,
        machine_id: str | None = None,
        pool: str = "default",
        labels: dict[str, str] | None = None,
    ) -> Worker:
        with self.context.database.session() as session:
            workspace_id = self.context.default_workspace_id(session)
            worker = WorkerRepository(session).records.create(
                {
                    "machine_id": optional_uuid(machine_id, field="machine_id"),
                    "pool": pool,
                    "labels": labels or {},
                    "status": ResourceStatus.Running.value,
                },
                workspace_id=workspace_id,
                status=ResourceStatus.Running.value,
            )
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeWorkers,
            change=WorkspaceChangeType.Created,
            resource_id=worker.id,
        )
        return worker

    def list_workers(self) -> list[Worker]:
        with self.context.database.session() as session:
            records = WorkerRepository(session).list_across_workspaces()
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def set_worker_status(self, worker_id: str, status: ResourceStatus) -> Worker:
        with self.context.database.session() as session:
            repository = WorkerRepository(session)
            worker = repository.get_across_workspaces(worker_id)
            if worker is None:
                msg = f"worker not found: {worker_id}"
                raise KeyError(msg)
            workspace_id = repository.workspace_id(worker_id)
            changed = worker.status is not status
            worker.status = status
            worker.last_seen_at = utc_now()
            updated = repository.upsert(worker, workspace_id=workspace_id)
        if changed and workspace_id is not None:
            self._publish_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputeWorkers,
                change=WorkspaceChangeType.Updated,
                resource_id=worker_id,
            )
        return updated

    def delete_worker(self, worker_id: str) -> None:
        with self.context.database.session() as session:
            repository = WorkerRepository(session)
            workspace_id = repository.workspace_id(worker_id)
            repository.records.delete_across_workspaces(worker_id)
        if workspace_id is not None:
            self._publish_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputeWorkers,
                change=WorkspaceChangeType.Deleted,
                resource_id=worker_id,
            )

    def _workspace_id(self) -> str:
        with self.context.database.session() as session:
            return self.context.default_workspace_id(session)

    def _publish_change(
        self,
        *,
        workspace_id: str,
        topic: WorkspaceChangeTopic,
        change: WorkspaceChangeType,
        resource_id: str,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=workspace_id,
            topic=topic,
            change=change,
            resource_id=resource_id,
        )

    def _provider_clients_for(
        self,
        providers: list[str],
        clients: Mapping[str, DirectMachineProvider],
    ) -> dict[str, DirectMachineProvider]:
        if providers:
            missing = [provider for provider in providers if provider not in clients]
            if missing:
                msg = f"compute provider unavailable: {', '.join(sorted(missing))}"
                raise ManagedComputeLaunchError(msg, code="provider_unavailable")
            return {provider: clients[provider] for provider in providers}
        return dict(sorted(clients.items()))

    def _provider_client_snapshot(
        self,
        workspace: str,
    ) -> Mapping[str, DirectMachineProvider]:
        if self.provider_registry is None:
            return {}
        return self.provider_registry.snapshot(workspace)

    def _provider_client_snapshot_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> Mapping[str, DirectMachineProvider]:
        if self.provider_registry is None:
            return {}
        return self.provider_registry.snapshot_for_workspace_deletion(workspace_id)

    def _resolved_workspace_providers(
        self,
        workspace_id: str,
        providers: list[str],
        *,
        clients: Mapping[str, DirectMachineProvider],
    ) -> dict[str, ResolvedComputeProvider]:
        if self.provider_resolver is None:
            return {}
        if providers:
            resolved: dict[str, ResolvedComputeProvider] = {}
            for provider_ref in providers:
                if provider_ref in clients:
                    continue
                resolved[provider_ref] = self.provider_resolver.resolve(
                    workspace_id,
                    provider_ref,
                )
            return resolved
        return {
            provider.ref: provider
            for provider in self.provider_resolver.list_providers(workspace_id)
        }

    def _internal_pool_provider(
        self,
        workspace_id: str,
        pool_name: str,
    ) -> tuple[ComputePoolRecord, ResolvedComputeProvider, ComputeOffer]:
        pool = self.get_internal_pool(workspace_id, pool_name)
        provider, offer = self._resolved_internal_pool_provider(pool)
        return pool, provider, offer

    def _resolved_internal_pool_provider(
        self,
        pool: ComputePoolRecord,
    ) -> tuple[ResolvedComputeProvider, ComputeOffer]:
        if self.provider_resolver is None:
            raise UpstreamUnavailableError("workspace compute provider resolver is not configured")
        try:
            provider = self.provider_resolver.resolve(pool.workspace_id, pool.provider_ref)
        except Exception as exc:
            raise UpstreamUnavailableError(
                f"compute pool {pool.name!r} provider is unavailable"
            ) from exc
        pooled = provider.pooled
        if pooled is None:
            raise InvalidInputError(f"compute pool {pool.name!r} provider is not pooled")
        offer = next(
            (
                item
                for item in pooled.list_offers()
                if item.id == pool.offer_id and item.region == pool.region
            ),
            None,
        )
        if offer is None:
            raise UpstreamUnavailableError(
                f"compute pool {pool.name!r} offer is no longer available"
            )
        return provider, offer

    def clear_capacity_degradation(
        self,
        workspace: str,
        pool_name: str,
    ) -> ComputePoolRecord:
        """Let a pool that exhausted its relaunch attempts buy machines again.

        Explicit because the degraded reason exists to stop a pool billing for
        machines that never become workers; anything that cleared it as a side
        effect would defeat it. The attempt baseline moves with it, since the
        ordinal it is compared against never resets on its own and the pool
        would degrade again on its next failure regardless of the cause.
        """
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = ComputePoolRepository(session)
            pool = repository.get_by_name(workspace_id, pool_name, for_update=True)
            if pool is None:
                raise NotFoundError(f"compute pool {pool_name!r} not found")
            machines = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
            highest = max(
                (record.launch_attempt for record in machines),
                default=pool.provider_state.launch_attempt_baseline,
            )
            cleared = repository.apply_provider_state(
                pool.id,
                generation=pool.generation,
                observed_machines=pool.observed_machines,
                phase=ComputePoolPhase.Ready,
                provider_state=pool.provider_state.model_copy(
                    update={"degraded_reason": None, "launch_attempt_baseline": highest}
                ),
            )
        if cleared is None:
            raise ConflictError(f"compute pool {pool_name!r} changed while clearing degradation")
        self._publish_change(
            workspace_id=cleared.workspace_id,
            topic=WorkspaceChangeTopic.ComputePools,
            change=WorkspaceChangeType.Updated,
            resource_id=cleared.id,
        )
        return cleared

    def _mark_pooled_capacity_degraded(
        self,
        pool: ComputePoolRecord,
        *,
        reason: str | None = None,
        preserve_deleting: bool = False,
    ) -> ComputePoolRecord | None:
        provider_state = (
            pool.provider_state.model_copy(update={"degraded_reason": reason})
            if reason is not None
            else pool.provider_state
        )
        with self.context.database.session() as session:
            degraded = ComputePoolRepository(session).apply_provider_state(
                pool.id,
                generation=pool.generation,
                observed_machines=pool.observed_machines,
                phase=(
                    ComputePoolPhase.Deleting
                    if preserve_deleting and pool.phase is ComputePoolPhase.Deleting
                    else ComputePoolPhase.Degraded
                ),
                provider_state=provider_state,
            )
        if degraded is not None:
            self._publish_change(
                workspace_id=degraded.workspace_id,
                topic=WorkspaceChangeTopic.ComputePools,
                change=WorkspaceChangeType.Updated,
                resource_id=degraded.id,
            )
        return degraded

    def _provider_pool_request(
        self,
        pool: ComputePoolRecord,
        offer: ComputeOffer,
    ) -> ProviderPoolRequest:
        if self.pool_bootstrap_factory is None or pool.provider_connection_id is None:
            raise RuntimeError("provider pool bootstrap is not configured")
        root_volume_gib = _pool_config_int(pool, "root_volume_gib", default=200)
        return ProviderPoolRequest(
            workspace_id=pool.workspace_id,
            pool_id=pool.id,
            pool_name=pool.name,
            provider_ref=pool.provider_ref,
            provider_connection_id=pool.provider_connection_id,
            generation=pool.generation,
            offer=offer,
            desired_machines=pool.desired_machines,
            max_machines=pool.max_machines,
            root_volume_gib=root_volume_gib,
            bootstrap=self.pool_bootstrap_factory.bootstrap(pool, offer),
            provider_state=pool.provider_state,
        )

    def _retire_proven_provider_pool_machines(
        self,
        pool: ComputePoolRecord,
        *,
        now: datetime,
    ) -> None:
        with self.context.database.session() as session:
            records = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        for record in records:
            if (
                record.machine_id
                and record.status == ReservationStatus.Deleted.value
                and _metadata_time(
                    _provider_instance_metadata(record),
                    "provider_storage_destroyed_at",
                )
                is not None
            ):
                self.provider_machines.retire_provider_pool_machine(
                    pool.workspace_id,
                    pool.name,
                    record.machine_id,
                    reason="provider instance storage destroyed",
                    now=now,
                )

    def retire_provider_pool_machines(
        self,
        workspace_id: str,
        pool_name: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        return self.provider_machines._retire_provider_pool_machines(
            workspace_id,
            pool_name,
            machine_ids=None,
            reason=reason,
            now=_utc(now),
        )

    def _ensure_compute_pool_record(
        self,
        repository: ComputePoolRepository,
        *,
        workspace_id: str,
        compute_pool: Pool,
        config: PoolConfig,
        source: str,
        now: datetime,
    ) -> ComputePoolRecord:
        existing = repository.get_by_name(workspace_id, compute_pool.name)
        payload: dict[str, JsonValue | datetime] = {
            "capacity_owner_id": compute_pool.capacity_owner_id,
            "capacity_owner_kind": compute_pool.capacity_owner_kind,
            "capacity_owner_source": compute_pool.capacity_owner_source,
            "workspace_id": workspace_id,
            "name": compute_pool.name,
            "selector": config.selector or compute_pool.name,
            "status": "active",
            "source": source,
            "config": _json_object(config),
            "expires_at": (
                now + timedelta(seconds=compute_pool_from_config(config).ttl_seconds)
                if config.ttl
                else None
            ),
        }
        if existing is None:
            return repository.records.create(
                payload,
                workspace_id=workspace_id,
                name=compute_pool.name,
                status="active",
            )
        return repository.upsert(existing.model_copy(update=payload))

    def _record_solver_run(
        self,
        session: DatabaseSession,
        *,
        workspace_id: str,
        pool_id: str,
        solve: ComputeSolvePlan,
    ) -> ComputeSolverRunRecord:
        return ComputeSolverRunRepository(session).records.create(
            {
                "workspace_id": workspace_id,
                "pool_id": pool_id,
                "feasible": solve.feasible,
                "reason": solve.reason,
                "metadata": _json_object(solve),
            },
            workspace_id=workspace_id,
        )

    def _record_solver_decision(
        self,
        session: DatabaseSession,
        solver_run: ComputeSolverRunRecord,
        action: ComputeSolveAction,
    ) -> None:
        offer = action.offer
        reservation = action.reservation
        ComputeSolverDecisionRepository(session).records.create(
            {
                "solver_run_id": solver_run.id,
                "action": action.action.value,
                "provider": offer.provider if offer is not None else None,
                "offer_id": offer.id if offer is not None else None,
                "reservation_id": reservation.id if reservation is not None else None,
                "count": action.count,
                "cost_micros": action.cost_micros,
                "reason": action.reason,
            },
            status=action.action.value,
        )

    def _check_launch_credit(
        self,
        workspace_id: str,
        pool_name: str,
        solve: ComputeSolvePlan,
    ) -> None:
        quantity = sum(
            action.count for action in solve.actions if action.action is SolveActionType.Create
        )
        if quantity <= 0:
            return
        hourly_cost = sum(
            (action.offer.hourly_cost_micros if action.offer is not None else 0) * action.count
            for action in solve.actions
            if action.action is SolveActionType.Create
        )
        decision = self.billing.check_launch_credit(
            BillingCreditRequest(
                workspace_id=workspace_id,
                pool_name=pool_name,
                quantity=quantity,
                estimated_hourly_cost_micros=hourly_cost,
                estimated_committed_micros=solve.committed_cost_micros,
            )
        )
        if decision.ok:
            return
        msg = decision.message or "not enough credits to launch managed compute"
        raise ManagedComputeLaunchError(
            msg,
            code=decision.error_code or "insufficient_credits",
            decision=decision,
        )

    def _create_provider_machine(
        self,
        session: DatabaseSession,
        *,
        workspace_id: str,
        pool_name: str,
        provider_name: str,
        offer: ComputeOffer,
    ) -> Machine:
        return MachineRepository(session).records.create(
            {
                "pool": pool_name,
                "provider": provider_name,
                "cpu": offer.cpu_millicores / 1000 if offer.cpu_millicores else None,
                "memory": f"{offer.memory_mb}Mi" if offer.memory_mb else None,
                "gpu": offer.gpu,
                "labels": {
                    "source": ComputePoolSource.Managed.value,
                    "offer_id": offer.id,
                    "instance_type": offer.instance_type,
                    "region": offer.region,
                    "gpu_count": str(offer.gpu_count),
                },
                "status": ResourceStatus.Created.value,
            },
            workspace_id=workspace_id,
            status=ResourceStatus.Created.value,
        )

    def _update_machine_after_launch(
        self,
        session: DatabaseSession,
        machine: Machine,
        *,
        provider_instance_id: str,
        status: str,
        address: str,
    ) -> Machine:
        resource_status = _resource_status_from_provider(status)
        updated = machine.model_copy(
            update={
                "address": address or machine.address,
                "status": resource_status,
                "updated_at": utc_now(),
                "labels": {
                    **machine.labels,
                    "provider_instance_id": provider_instance_id,
                },
            }
        )
        return MachineRepository(session).upsert(updated)

    def _private_pool_state_from_records(
        self,
        pool: ComputePoolRecord,
        instances: list[ComputeProviderInstanceRecord],
    ) -> PrivatePoolState:
        reservations = [_provider_reservation_from_record(pool.name, item) for item in instances]
        open_reservations = [item for item in reservations if _reservation_open(item.status)]
        return PrivatePoolState(
            capacity_owner_id=pool.capacity_owner_id,
            capacity_owner_kind=pool.capacity_owner_kind,
            capacity_owner_source=pool.capacity_owner_source,
            workspace_id=pool.workspace_id,
            name=pool.name,
            selector=pool.selector or pool.name,
            config=(
                PoolConfig.model_validate(pool.config)
                if pool.config
                else PoolConfig(name=pool.name)
            ),
            reservations=reservations,
            committed_spend_micros=sum(item.committed_micros for item in reservations),
            status=pool.status,
            source=pool.source,
            created_by_token_id=(
                str(pool.config.get("created_by_token_id", "")) if pool.config else ""
            ),
            created_at=None,
            updated_at=None,
            expires_at=pool.expires_at,
            reserved_nodes=sum(max(item.node_count, 1) for item in open_reservations),
        )

    def reconcile_provider_capacity(self, *, now: datetime | None = None) -> list[PrivatePoolState]:
        current_time = _utc(now)
        self.reconcile_pooled_capacity(now=current_time)
        workspace_id = self._workspace_id()
        clients = self._provider_client_snapshot(workspace_id)
        reconciled: list[PrivatePoolState] = []
        changed_states: list[PrivatePoolState] = []
        machine_changes: dict[str, WorkspaceChangeType] = {}
        with self.context.database.session() as session:
            compute_pools = ComputePoolRepository(session).records.list(workspace_id=workspace_id)
            provider_instances = ComputeProviderInstanceRepository(session)
            for compute_pool in compute_pools:
                if compute_pool.visibility is ComputePoolVisibility.Internal:
                    continue
                instances = provider_instances.list_for_pool(compute_pool.id)
                billing_changed, deleted_machine_ids = self._reconcile_pool_billing(
                    session,
                    workspace_id=workspace_id,
                    pool=compute_pool,
                    instances=instances,
                    now=current_time,
                    clients=clients,
                )
                if billing_changed:
                    instances = provider_instances.list_for_pool(compute_pool.id)
                provider_changed, updated_machine_ids, reclaimed_machine_ids = (
                    self.provider_machines._reconcile_provider_machines(
                        session,
                        compute_pool,
                        instances,
                        clients=clients,
                        now=current_time,
                    )
                )
                changed = billing_changed or provider_changed
                for machine_id in updated_machine_ids:
                    machine_changes[machine_id] = WorkspaceChangeType.Updated
                for machine_id in deleted_machine_ids | reclaimed_machine_ids:
                    machine_changes[machine_id] = WorkspaceChangeType.Deleted
                refreshed = provider_instances.list_for_pool(compute_pool.id)
                state = self._private_pool_state_from_records(compute_pool, refreshed)
                if changed and self.scheduler_hooks is not None:
                    self.scheduler_hooks.register_pool(state)
                reconciled.append(state)
                if changed:
                    changed_states.append(state)
        for state in changed_states:
            self._publish_change(
                workspace_id=state.workspace_id,
                topic=WorkspaceChangeTopic.ComputePools,
                change=WorkspaceChangeType.Updated,
                resource_id=state.name,
            )
        for machine_id, change in machine_changes.items():
            self._publish_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputeMachines,
                change=change,
                resource_id=machine_id,
            )
        return reconciled

    def _reconcile_pool_billing(
        self,
        session: DatabaseSession,
        *,
        workspace_id: str,
        pool: ComputePoolRecord,
        instances: list[ComputeProviderInstanceRecord],
        now: datetime,
        clients: Mapping[str, DirectMachineProvider],
    ) -> tuple[bool, set[str]]:
        open_instances = [item for item in instances if _reservation_open(item.status)]
        if not open_instances:
            return False, set()
        changed = False
        deleted_machine_ids: set[str] = set()
        launch_compensations = [
            record
            for record in open_instances
            if record.status == ReservationStatus.Terminating.value
            and _provider_launch_state(record) == _LAUNCH_STATE_COMPENSATING
        ]
        for record in launch_compensations:
            terminated = self.provider_machines._terminate_provider_record(
                session,
                record,
                clients=clients,
                reason="launch_compensation_retry",
                message="retrying failed provider launch compensation",
            )
            changed = terminated or changed
            if terminated and record.machine_id:
                deleted_machine_ids.add(record.machine_id)
        compensation_ids = {record.id for record in launch_compensations}
        open_instances = [record for record in open_instances if record.id not in compensation_ids]
        if not open_instances:
            return changed, deleted_machine_ids
        capacity_requests = ComputeCapacityRequestRepository(session)
        capacity = capacity_requests.active_for_pool(pool.id, for_update=True)
        capacity_expired = (
            capacity is not None and capacity.expires_at is not None and now >= capacity.expires_at
        )
        expired_instances = (
            open_instances
            if capacity_expired
            else (
                []
                if capacity is not None and capacity.expires_at is not None
                else [
                    record
                    for record in open_instances
                    if record.expires_at is not None and now >= record.expires_at
                ]
            )
        )
        for record in expired_instances:
            terminated = self.provider_machines._terminate_provider_record(
                session,
                record,
                clients=clients,
                reason="capacity_deadline_exceeded",
                message="managed compute pool capacity deadline expired",
            )
            changed = terminated or changed
            if terminated and record.machine_id:
                deleted_machine_ids.add(record.machine_id)
        if capacity_expired and capacity is not None:
            capacity_requests.upsert(capacity.model_copy(update={"status": "expired"}))
        expired_ids = {record.id for record in expired_instances}
        open_instances = [record for record in open_instances if record.id not in expired_ids]
        if not open_instances:
            return changed, deleted_machine_ids
        for record in open_instances:
            self._record_managed_usage(
                session,
                workspace_id=workspace_id,
                pool_name=pool.name,
                record=record,
                now=now,
            )
        refreshed_by_id = {
            record.id: record
            for record in ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        }
        open_instances = [refreshed_by_id.get(record.id, record) for record in open_instances]
        if capacity is not None and capacity.expires_at is not None:
            projected_records = [
                _provider_record_at_deadline(record, capacity.expires_at)
                for record in open_instances
            ]
            projected_commitment = sum(
                record.committed_micros
                for record in projected_records
                if record.source == ComputePoolSource.Managed.value
            )
            current_spend = _recorded_pool_spend_micros(
                session,
                workspace_id=workspace_id,
                pool_id=pool.id,
            )
            required_budget = max(projected_commitment, current_spend)
            if required_budget > capacity.max_spend_micros:
                for record in open_instances:
                    terminated = self.provider_machines._terminate_provider_record(
                        session,
                        record,
                        clients=clients,
                        reason="capacity_budget_exceeded",
                        message="managed compute pool capacity budget exhausted",
                    )
                    changed = terminated or changed
                    if terminated and record.machine_id:
                        deleted_machine_ids.add(record.machine_id)
                return changed, deleted_machine_ids
            aligned_open_instances: list[ComputeProviderInstanceRecord] = []
            for projected in projected_records:
                refreshed = refreshed_by_id.get(projected.id, projected)
                aligned = _provider_record_at_deadline(refreshed, capacity.expires_at)
                if aligned != refreshed:
                    ComputeProviderInstanceRepository(session).upsert(aligned)
                    changed = True
                aligned_open_instances.append(aligned)
            open_instances = aligned_open_instances
        decision = self.billing.check_balance(workspace_id)
        if not decision.ok:
            for record in open_instances:
                terminated = self.provider_machines._terminate_provider_record(
                    session,
                    record,
                    clients=clients,
                    reason=decision.error_code or "credit_exhausted",
                    message=decision.message or "managed compute credits exhausted",
                )
                changed = terminated or changed
                if terminated and record.machine_id:
                    deleted_machine_ids.add(record.machine_id)
            return changed, deleted_machine_ids
        return changed, deleted_machine_ids

    def _record_managed_usage(
        self,
        session: DatabaseSession,
        *,
        workspace_id: str,
        pool_name: str,
        record: ComputeProviderInstanceRecord,
        now: datetime,
        deleting_workspace_id: str | None = None,
    ) -> bool:
        record_metadata = _provider_instance_metadata(record)
        cursor = _metadata_time(record_metadata, "billing_cursor_at") or record.expires_at or now
        end = min(now, record.expires_at) if record.expires_at is not None else now
        if end <= cursor:
            return False
        duration_seconds = (end - cursor).total_seconds()
        cost_cents = managed_cost_cents(record.hourly_cost_micros, duration_seconds)
        usage = ManagedUsage(
            idempotency_key=usage_record_id(
                UsageMetric.ManagedComputeReservationCostCents.value,
                workspace_id,
                record.id,
                cursor.isoformat(),
                end.isoformat(),
            ),
            workspace_id=workspace_id,
            pool_name=pool_name,
            reservation_id=record.id,
            provider=record.provider,
            cloud=str(record_metadata.get("cloud") or record.provider),
            provider_instance_id=record.instance_id or record.id,
            machine_id=record.machine_id or "",
            gpu=record.gpu or "",
            gpu_count=record.gpu_count,
            node_count=_metadata_int(record_metadata, "node_count", default=1),
            cpu_millicores=record.cpu_millicores,
            memory_mb=record.memory_mb,
            storage_mb=_metadata_int(record_metadata, "storage_mb"),
            hourly_cost_micros=record.hourly_cost_micros,
            duration_seconds=duration_seconds,
            cost_cents=cost_cents,
            start_at=cursor,
            end_at=end,
        )
        self.billing.record_usage(usage)
        export_error = self._export_managed_usage_metrics(usage)
        usage_repository = UsageRepository(session)
        record_usage = (
            usage_repository.record_for_workspace_deletion
            if deleting_workspace_id is not None
            else usage_repository.record
        )
        record_usage(
            id=usage_record_id(
                UsageMetric.ManagedComputeReservationCostCents.value,
                workspace_id,
                record.id,
                cursor.isoformat(),
                end.isoformat(),
            ),
            workspace_id=workspace_id,
            resource_type="managed_compute_reservation",
            resource_id=record.id,
            metric=UsageMetric.ManagedComputeReservationCostCents,
            quantity=cost_cents,
            unit=UsageUnit.Cents,
            labels={"provider": record.provider, "pool": pool_name},
            metadata=_json_object(usage),
        )
        record_usage(
            id=usage_record_id(
                UsageMetric.ManagedComputeReservationSeconds.value,
                workspace_id,
                record.id,
                cursor.isoformat(),
                end.isoformat(),
            ),
            workspace_id=workspace_id,
            resource_type="managed_compute_reservation",
            resource_id=record.id,
            metric=UsageMetric.ManagedComputeReservationSeconds,
            quantity=duration_seconds,
            unit=UsageUnit.Seconds,
            labels={"provider": record.provider, "pool": pool_name},
            metadata=_json_object(usage),
        )
        ledger_repository = ComputeLedgerRepository(session)
        ledger_record = ComputeLedgerRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            pool_id=record.pool_id,
            reservation_id=record.id,
            source="managed_compute",
            amount_micros=int(cost_cents * 10_000),
            started_at=cursor,
            ended_at=end,
            metadata=_json_object(usage),
        )
        if deleting_workspace_id is not None:
            ledger_repository.append_for_workspace_deletion(ledger_record)
        else:
            ledger_repository.append(ledger_record)
        next_metadata: dict[str, JsonValue] = {
            **record_metadata,
            "billing_cursor_at": end.isoformat(),
        }
        if export_error:
            next_metadata["last_usage_export_error"] = export_error
        else:
            next_metadata.pop("last_usage_export_error", None)
        updated = record.model_copy(update={"metadata": next_metadata})
        ComputeProviderInstanceRepository(session).upsert(updated)
        return True

    def _export_managed_usage_metrics(self, usage: ManagedUsage) -> str:
        if self.usage_exporter is None:
            return ""
        metadata = _json_object(usage)
        try:
            self.usage_exporter.emit(
                name=UsageMetric.ManagedComputeReservationSeconds.value,
                metadata=metadata,
                value=usage.duration_seconds,
            )
            self.usage_exporter.emit(
                name=UsageMetric.ManagedComputeReservationCostCents.value,
                metadata=metadata,
                value=usage.cost_cents,
            )
        except Exception as exc:
            return str(exc)
        return ""

    def _compensate_failed_launch(
        self,
        created_instances: list[LaunchedProviderInstance],
        *,
        clients: Mapping[str, DirectMachineProvider],
    ) -> set[str]:
        """Terminate instances created by a failed launch at their owning provider.

        Each instance is terminated only through the provider client that
        launched it. Durable launch intents remain available to reconciliation
        when immediate termination fails.
        """
        terminated_machine_ids: set[str] = set()
        for instance in created_instances:
            client = clients.get(instance.provider)
            if client is None:
                LOGGER.error(
                    "launch compensation has no provider client; instance may leak",
                    extra={
                        "provider": instance.provider,
                        "machine_id": instance.machine_id,
                        "provider_instance_id": instance.provider_instance_id,
                    },
                )
                continue
            try:
                client.terminate_machine(instance.provider_instance_id)
                if client.machine_storage_destroyed(
                    instance.provider_instance_id,
                    instance.storage_volume_ids,
                ):
                    observed_at = utc_now()
                    if self.source_cache_lifecycle.record_machine_storage_destroyed(
                        instance.machine_id,
                        observed_at=observed_at,
                    ):
                        terminated_machine_ids.add(instance.machine_id)
            except Exception:
                LOGGER.exception(
                    "launch compensation failed to terminate provider machine",
                    extra={
                        "provider": instance.provider,
                        "machine_id": instance.machine_id,
                        "provider_instance_id": instance.provider_instance_id,
                    },
                )
        return terminated_machine_ids


def _pool_labels_from_config(config: PoolConfig) -> dict[str, str]:
    labels = {
        "selector": config.selector,
        "mode": str(config.mode),
        "transport": str(config.transport),
        "fallback": str(config.fallback),
        "priority": str(config.priority),
    }
    return {key: value for key, value in labels.items() if value}


class _CapacityJoinReuse(Enum):
    """Whether an existing provider-launch credential can still serve this operation."""

    Usable = "usable"
    Renew = "renew"
    Rejected = "rejected"


def _capacity_join_credential_reuse(
    credential: ComputeJoinCredentialRecord | None,
    *,
    workspace_id: str,
    pool_name: str,
    machine_id: str,
    now: datetime,
) -> _CapacityJoinReuse:
    if credential is None:
        return _CapacityJoinReuse.Usable
    scoped_to_operation = (
        credential.workspace_id == workspace_id
        and credential.pool_name == pool_name
        and credential.machine_id == machine_id
    )
    if not scoped_to_operation or credential.use_count != 0:
        # A consumed credential means a machine already enrolled on this authority;
        # minting another would admit a second enrollment for the same machine.
        return _CapacityJoinReuse.Rejected
    if credential.status is not ComputeCredentialStatus.Active or credential.expires_at <= now:
        return _CapacityJoinReuse.Renew
    return _CapacityJoinReuse.Usable


def _capacity_launch_idempotency_key(*, operation_id: str, join_attempt: int) -> str:
    """Provider idempotency key for one launch attempt of a capacity operation.

    Separate from the operation's logical identity: retries inside an attempt reuse
    the key so the provider treats them as replays, while a compensated attempt
    advances it so the next launch is a genuinely new side effect.
    """
    return f"{operation_id}\0{join_attempt}"


def _plan_capacity_join_token(
    signing_key: str,
    *,
    principal: ComputePrincipal,
    pool_name: str,
    operation_id: str,
    machine_id: str,
    join_attempt: int,
) -> JoinTokenCreationPlan:
    return plan_join_token_creation(
        principal,
        pool_name,
        machine_id=machine_id,
        token=_capacity_join_token(
            signing_key,
            operation_id=operation_id,
            machine_id=machine_id,
            join_attempt=join_attempt,
        ),
    )


def _capacity_join_token(
    signing_key: str,
    *,
    operation_id: str,
    machine_id: str,
    join_attempt: int,
) -> str:
    payload = "\0".join(
        ("capacity-provider-join-v1", operation_id, machine_id, str(join_attempt))
    ).encode()
    digest = hmac.new(signing_key.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _offer_matches_capacity_policy(offer: ComputeOffer, pool: Pool) -> bool:
    return (
        offer.capacity_mode
        is (
            ComputeCapacityMode.Pooled
            if pool.capacity_owner_kind is CapacityOwnerKind.PooledProvider
            else ComputeCapacityMode.Direct
        )
        and offer.cpu_millicores == pool.worker_cpu_millicores
        and offer.memory_mb == pool.worker_memory_mib
        and (offer.gpu or "") == pool.worker_gpu_type
        and offer.gpu_count == pool.worker_gpu_count
        and offer.runtime in pool.worker_runtimes
    )


def _shape_matches_pool(shape: CapacityAcquisitionShape, pool: Pool) -> bool:
    return (
        shape.cpu_millicores == pool.worker_cpu_millicores
        and shape.memory_mib == pool.worker_memory_mib
        and shape.gpu_type == pool.worker_gpu_type
        and shape.gpu_count == pool.worker_gpu_count
        and shape.runtime in pool.worker_runtimes
        and shape.preemptible is pool.worker_preemptible
    )


def _offer_matches_capacity_shape(
    offer: ComputeOffer,
    shape: CapacityAcquisitionShape,
) -> bool:
    preemptible = str(offer.labels.get("preemptible", "false")).strip().lower() == "true"
    return (
        offer.cpu_millicores == shape.cpu_millicores
        and offer.memory_mb == shape.memory_mib
        and (offer.gpu or "") == shape.gpu_type
        and offer.gpu_count == shape.gpu_count
        and offer.runtime == shape.runtime
        and preemptible is shape.preemptible
    )


def _new_capacity_operation(
    pool: ComputePoolRecord,
    request: CapacityAcquisitionRequest,
    *,
    desired_unit: int,
    status: str,
    previous_desired_unit: int,
    target_machine_id: str | None = None,
    owns_capacity: bool = False,
    last_error: str = "",
) -> ComputeCapacityOperationRecord:
    now = utc_now()
    return ComputeCapacityOperationRecord(
        id=str(uuid5(NAMESPACE_URL, f"capacity-operation\0{request.operation_id}")),
        workspace_id=pool.workspace_id,
        pool_id=pool.id,
        capacity_owner_id=request.capacity_owner_id,
        reservation_id=request.reservation_id,
        operation_id=request.operation_id,
        desired_unit=desired_unit,
        status=status,
        target_machine_id=target_machine_id,
        previous_desired_unit=previous_desired_unit,
        owns_capacity=owns_capacity,
        shape=_json_object(request.shape),
        last_error=last_error,
        created_at=now,
        updated_at=now,
    )


def _validate_capacity_operation(
    operation: ComputeCapacityOperationRecord,
    request: CapacityAcquisitionRequest,
    *,
    desired_unit: int,
) -> None:
    if (
        operation.reservation_id != request.reservation_id
        or operation.desired_unit != desired_unit
        or operation.shape != _json_object(request.shape)
    ):
        raise ConflictError(f"capacity operation request is immutable: {request.operation_id}")


def _validate_capacity_operation_plan(
    operation: ComputeCapacityOperationRecord,
    request: CapacityAcquisitionRequest,
) -> None:
    if operation.reservation_id != request.reservation_id or operation.shape != _json_object(
        request.shape
    ):
        raise ConflictError(f"capacity operation request is immutable: {request.operation_id}")


def _stored_capacity_status(status: str) -> CapacityAcquisitionStatus:
    if status in {"intent", "releasing"}:
        return CapacityAcquisitionStatus.ExistingPending
    if status == "released":
        return CapacityAcquisitionStatus.Unsupported
    try:
        return CapacityAcquisitionStatus(status)
    except ValueError:
        return CapacityAcquisitionStatus.TemporarilyUnavailable


def _capacity_result(
    request: CapacityAcquisitionRequest,
    status: CapacityAcquisitionStatus,
    *,
    desired_unit: int,
    reason: str = "",
    failure_code: CapacityFailureCode | None = None,
) -> CapacityAcquisitionResult:
    return CapacityAcquisitionResult(
        status=status,
        capacity_owner_id=request.capacity_owner_id,
        reservation_id=request.reservation_id,
        desired_unit=max(desired_unit, 1),
        failure_code=failure_code,
        reason=reason,
    )


def _plan_next_capacity_unit(
    request: CapacityAcquisitionRequest,
    *,
    current_units: int,
    max_units: int,
) -> CapacityAcquisitionResult:
    if current_units >= max_units:
        return _capacity_result(
            request,
            CapacityAcquisitionStatus.AtLimit,
            desired_unit=max(current_units, 1),
            reason=(f"capacity limit reached: {current_units} units held, maximum is {max_units}"),
        )
    return _capacity_result(
        request,
        CapacityAcquisitionStatus.Requested,
        desired_unit=current_units + 1,
        reason="planned one bounded capacity unit",
    )


def _operation_result(
    operation: ComputeCapacityOperationRecord,
    status: CapacityAcquisitionStatus,
    *,
    reason: str = "",
    failure_code: CapacityFailureCode | None = None,
) -> CapacityAcquisitionResult:
    return CapacityAcquisitionResult(
        status=status,
        capacity_owner_id=operation.capacity_owner_id,
        reservation_id=operation.reservation_id,
        desired_unit=operation.desired_unit,
        target_machine_id=operation.target_machine_id,
        failure_code=failure_code or operation.failure_code,
        reason=reason or operation.last_error,
    )


def _offer_matches_pool(offer: ComputeOffer, plan: ComputePoolPlan) -> bool:
    if plan.offer_id and offer.id != plan.offer_id:
        return False
    if plan.providers and offer.provider not in plan.providers:
        return False
    if plan.regions and offer.region not in plan.regions:
        return False
    if plan.gpu and offer.gpu not in plan.gpu:
        return False
    if plan.gpu and offer.gpu_count <= 0:
        return False
    if not plan.gpu and plan.nodes > 0 and offer.gpu_count > 0:
        return False
    if plan.min_reliability > 0 and offer.reliability > 0:
        return offer.reliability >= plan.min_reliability
    return offer.available > 0


def _offer_cost(offer: ComputeOffer) -> float:
    capacity = offer.node_count or 1
    return offer.hourly_cost_micros / max(capacity, 1)


def _resource_status_from_provider(status: str) -> ResourceStatus:
    if status == ProviderMachineStatus.Active:
        return ResourceStatus.Running
    if status == ProviderMachineStatus.Unhealthy:
        return ResourceStatus.Failed
    if status == ProviderMachineStatus.Terminated:
        return ResourceStatus.Deleted
    return ResourceStatus.Created


def _provider_reservation_from_record(
    pool_name: str,
    record: ComputeProviderInstanceRecord,
) -> ProviderReservation:
    metadata = _provider_instance_metadata(record)
    return ProviderReservation(
        id=record.id,
        pool_name=pool_name,
        selector=str(metadata.get("selector") or pool_name),
        provider=record.provider,
        cloud=str(metadata.get("cloud") or record.provider),
        region=str(metadata.get("region") or ""),
        offer_id=record.offer_id,
        instance_type=record.instance_type or "",
        instance_id=record.instance_id or "",
        status=record.status,
        gpu=record.gpu,
        gpu_count=record.gpu_count,
        hourly_cost_micros=record.hourly_cost_micros,
        committed_micros=record.committed_micros,
        source=record.source,
        created_at=_provider_commitment_start(record),
        expires_at=record.expires_at,
        billing_renewal_at=record.billing_renewal_at,
        billing_cursor_at=_metadata_time(metadata, "billing_cursor_at"),
        status_message=str(metadata.get("status_message") or ""),
        terminating_reason=str(metadata.get("terminating_reason") or ""),
        last_error=str(metadata.get("last_error") or ""),
        registration_token_hash=str(metadata.get("registration_token_hash") or ""),
        machine_id=record.machine_id or "",
        node_count=_metadata_int(metadata, "node_count", default=1),
        cpu_millicores=record.cpu_millicores,
        memory_mb=record.memory_mb,
        storage_mb=_metadata_int(metadata, "storage_mb"),
        architecture=str(metadata.get("architecture") or ""),
        runtime=str(metadata.get("runtime") or ""),
    )


def _compute_reservation_from_record(
    reservation: ProviderReservation,
    *,
    pool_name: str,
) -> ComputeReservation:
    return ComputeReservation(
        id=reservation.id,
        pool_name=pool_name,
        selector=reservation.selector,
        provider=reservation.provider,
        cloud=reservation.cloud,
        region=reservation.region,
        offer_id=reservation.offer_id,
        instance_type=reservation.instance_type,
        instance_id=reservation.instance_id,
        machine_id=reservation.machine_id,
        gpu=reservation.gpu,
        gpu_count=reservation.gpu_count,
        node_count=reservation.node_count,
        cpu_millicores=reservation.cpu_millicores,
        memory_mb=reservation.memory_mb,
        storage_mb=reservation.storage_mb,
        architecture=reservation.architecture,
        runtime=reservation.runtime,
        hourly_cost_micros=reservation.hourly_cost_micros,
        committed_micros=reservation.committed_micros,
        source=(
            ReservationSource.Attached
            if str(reservation.source) == ComputePoolSource.Attached.value
            else ReservationSource.CliReservation
        ),
        status=reservation.status,
        created_at_seconds=_unix_seconds(reservation.created_at),
        expires_at_seconds=_unix_seconds(reservation.expires_at),
        billing_renewal_at_seconds=_unix_seconds(reservation.billing_renewal_at),
    )


def _json_object(model: ContractModel) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_json(model.model_dump_json())


def _metadata_int(metadata: Mapping[str, JsonValue], key: str, *, default: int = 0) -> int:
    value = metadata.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        return int(value or default)
    return default


def _provider_record_at_deadline(
    record: ComputeProviderInstanceRecord,
    deadline: datetime,
) -> ComputeProviderInstanceRecord:
    committed_micros = record.committed_micros
    if record.source == ComputePoolSource.Managed.value:
        lifetime_seconds = ceil(
            (_utc(deadline) - _provider_commitment_start(record)).total_seconds()
        )
        projected = record.hourly_cost_micros * _whole_hours(lifetime_seconds)
        committed_micros = max(committed_micros, projected)
    return record.model_copy(
        update={
            "expires_at": _utc(deadline),
            "committed_micros": committed_micros,
        }
    )


def _provider_commitment_start(record: ComputeProviderInstanceRecord) -> datetime:
    return _metadata_time(
        _provider_instance_metadata(record),
        "commitment_started_at",
    ) or _utc(record.created_at)


def _recorded_pool_spend_micros(
    session: DatabaseSession,
    *,
    workspace_id: str,
    pool_id: str,
) -> int:
    return sum(
        max(record.amount_micros, 0)
        for record in ComputeLedgerRepository(session).list_for_workspace(workspace_id)
        if record.pool_id == pool_id and record.source == "managed_compute"
    )


def _policy_owned_scale(pool: ComputePoolRecord) -> None:
    """Workspace policy owns the zero-capacity intent; no extra scale guard applies."""
    del pool


def _previous_policy_floor(
    current: ComputePoolRecord | None,
    current_policy: Pool | None,
) -> int:
    """The floor the stored pool was already holding.

    ``initial_workers`` matters as well as ``min_machines``: a policy whose
    initial exceeds its minimum holds that capacity durably, so reading only the
    minimum would under-release it and leave paid machines behind.
    """

    if current is None:
        return 0
    initial = current_policy.initial_workers if current_policy is not None else 0
    return max(current.min_machines, initial, 0)


def _policy_owned_desired_machines(
    *,
    current_desired: int,
    previous_floor: int,
    floor: int,
    ceiling: int,
) -> int:
    """Resolve policy-owned desired capacity within the policy's own bounds.

    The workspace policy owns the floor and the ceiling; growth above the floor
    belongs to the scheduler's autoscaling and drain owners. Releasing exactly
    the capacity a lowered floor was holding keeps this idempotent under an
    unchanged policy, which control-plane startup depends on: it reconciles every
    active workspace through this path on every boot, and a plain clamp to the
    floor would discard demand-grown capacity each time.
    """

    released = max(previous_floor - floor, 0)
    return min(max(current_desired - released, floor), ceiling)


def _zero_capacity_converged(
    pool: ComputePoolRecord,
    sizing_state: CapacityPoolSizingState,
) -> bool:
    return (
        pool.desired_machines == 0
        and pool.observed_machines == 0
        and pool.phase is ComputePoolPhase.Ready
        and sizing_state.initial_target_reached
        and sizing_state.operation_id == ""
        and sizing_state.target_units == 0
        and sizing_state.operation_started_at is None
        and sizing_state.last_scale_down_at is not None
        and sizing_state.retry_after_at is None
        and sizing_state.consecutive_failures == 0
        and sizing_state.terminal_reason == ""
    )


def _pool_gpu_capacity(pool: ComputePoolRecord) -> bool:
    return _pool_config_int(pool, "gpu_count", default=0) > 0


def _unix_seconds(value: datetime | None) -> int:
    if value is None:
        return 0
    return int(_utc(value).timestamp())
