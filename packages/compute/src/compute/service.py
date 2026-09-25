from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.capacity_recovery import CapacityRecoveryRepository
from database.repositories.compute import (
    ComputeCapacityOperationRecord,
    ComputeCapacityOperationRepository,
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentRepository,
    ComputeOfferState,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeReserveInstance,
    ComputeUnitRepository,
    PlatformReserveUnitRow,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from database.repositories.worker_releases import WorkerReleaseRepository
from database.types import DatabaseSession
from observability.workspace_changes import WorkspaceChangePublisher
from shared.capacity import (
    CapacityAcquisitionRequest,
    CapacityAcquisitionResult,
    CapacityAcquisitionShape,
    CapacityAcquisitionStatus,
    CapacityFailureCode,
    CapacityFulfillmentRequest,
    CapacityOperationStatus,
    CapacityOwnerKind,
    CapacityOwnerSource,
    CapacityPoolSizingSnapshot,
    CapacityReleaseRequest,
    capacity_failure_message,
    capacity_owner_for_provider,
)
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputeCredentialStatus,
    ComputeMachineEnrollmentStatus,
    MachineBootstrapFailureReason,
    MachineStopPreparationReceipt,
    agent_machine_worker_id,
)
from shared.compute_fleet import Machine, MachineLifecycle, ResourceStatus, Worker
from shared.compute_policy import (
    ENDED_UNIT_PHASES,
    ComputeCapacityMode,
    ComputeResourceRequirements,
    ComputeUnitPhase,
    ComputeUnitProviderState,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    UnitName,
)
from shared.compute_reconciliation import (
    COMPUTE_RECONCILIATION_BATCH_SIZE,
    ComputeReconciliationKind,
)
from shared.container_requests import OciRuntimeName, node_fits_request
from shared.containers import LIVE_CONTAINER_STATUSES, ContainerStatus
from shared.errors import (
    CapacityLimitReachedError,
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.gpu import GPU_ANY, gpu_preference_accepts, normalize_gpu_type
from shared.http.worker_network import WorkerEgressPolicy
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import WorkspaceStatus
from shared.placement import Placement
from shared.releases import ReleaseTarget
from shared.routing import PrivateUnitFallback
from shared.timestamps import to_utc, utc_now
from shared.usage import UsageBillingOwner

from compute.aws_connections import AwsAccountPoolDrain
from compute.capacity_errors import (
    CapacityReservationConflictError,
    CapacityReservationLeaseLostError,
    CapacityReservationLockContendedError,
    ProviderAuthorizationPendingError,
)
from compute.context import ComputeContext
from compute.fleet_policy import (
    Capacity,
    FleetCapacityPolicy,
    FleetReservePlan,
    FleetReserveSnapshot,
    GrowthKind,
    MachineRole,
    MarketReservePlan,
    ReserveConditions,
    ReserveMachineState,
    ReserveMarket,
    plan_market_reserve,
)
from compute.fleet_reserves import (
    ReserveAdmission,
    fleet_reserve_snapshot,
    machine_capacity,
    reserve_admission,
    unit_reserve_market,
)
from compute.machine_lifecycle import (
    PREPARED_RESERVE_STATUSES,
    RETAINED_MACHINE_LIFECYCLES,
    machine_lifecycle_allowed,
    write_machine_lifecycle,
)
from compute.offers import (
    ComputeOffer,
    OfferRequest,
    ReservationStatus,
    choose_offer,
    cooling_regions,
    offer_selection_key,
    record_purchase_terms,
)
from compute.provider_machines import (
    ProviderMachineReconciler,
    ProviderUnitBootstrapFactory,
    _provider_zero_capacity_converged,
    _require_internal_pooled_unit,
    _reservation_open,
    _utc,
    provider_unit_operational_capacity,
    provider_unit_request,
)
from compute.providers import (
    CapacityOwnerMutationLease,
    ComputeProviderResolver,
    ComputeSchedulerHooks,
    DirectMachineProvider,
    DirectMachineProviderRegistry,
    PooledCapacityProvider,
    ProviderCapacityPhase,
    ProviderMachineStatus,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
    ResolvedComputeProvider,
    internal_unit_identity,
)
from compute.purchase_policy import assess_fleet_purchase
from compute.reclaim import ComputeReclaimPolicy
from compute.reserve_state import FleetReserveState
from compute.source_cache_storage import SourceCacheStorageLifecycleService
from compute.telemetry import AGENT_HEARTBEAT_TIMEOUT_SECONDS

LOGGER = logging.getLogger(__name__)
_RESERVE_REFRESH_CANDIDATES = 4
_RESERVE_OWNER = str(uuid5(NAMESPACE_URL, "lazycloud:platform-warm-capacity"))
"""The lease a reserve plan and a reserve refresh share, so neither runs beside the other."""


_UNCONFIRMED_OPERATION_STATUSES = frozenset(
    {
        CapacityOperationStatus.Intent,
        CapacityOperationStatus.TemporarilyUnavailable,
    }
)
"""Owning operations the provider has not acknowledged, so the unit is unbought.

An operation that reached `requested` is already counted by the provider's own
desired machines; one of these is not counted anywhere else and has to be
re-driven under its original id or the retry buys a second machine.
"""


class ManagedComputeLaunchError(DomainError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True, slots=True)
class _PooledCapacityBaseline:
    initial_machines: int
    min_machines: int
    min_free_cpu_millicores: int
    min_free_memory_mib: int


@dataclass(slots=True)
class ReserveAgentPreparation:
    preparing: bool = False
    warm: bool = False
    """The reserve's worker runs, fenced: it hibernates, or its resume is pending."""
    stop_request_id: str = ""
    resuming: bool = False
    lagging_resume: bool = False
    """The machine resumed before its row said so; its agent keeps the reserve as it is."""


@dataclass(slots=True)
class ComputeService:
    context: ComputeContext
    provider_registry: DirectMachineProviderRegistry | None = None
    provider_resolver: ComputeProviderResolver | None = None
    pool_bootstrap_factory: ProviderUnitBootstrapFactory | None = None
    scheduler_hooks: ComputeSchedulerHooks | None = None
    workspace_changes: WorkspaceChangePublisher | None = None
    capacity_owner_mutations: CapacityOwnerMutationLease | None = None
    reclaim: ComputeReclaimPolicy = field(default_factory=ComputeReclaimPolicy)
    fleet_policy: FleetCapacityPolicy = field(default_factory=FleetCapacityPolicy)
    reserve_state: FleetReserveState | None = None
    _reserve_decisions: dict[ReserveMarket, str] = field(
        default_factory=dict, init=False, repr=False
    )
    source_cache_lifecycle: SourceCacheStorageLifecycleService = field(init=False)

    def __post_init__(self) -> None:
        self.source_cache_lifecycle = SourceCacheStorageLifecycleService(self.context)

    def pooled_offer_rejection(
        self,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        *,
        preemptible: bool,
        now: datetime | None = None,
    ) -> str | None:
        policy = provider.policy
        if policy is None or offer.provider != provider.ref or not policy.accepts(offer):
            return "provider offer is outside its approved catalog"
        if not policy.can_purchase:
            return "provider purchases are disabled"
        if not policy.platform_fleet:
            return None
        assessment = assess_fleet_purchase(
            offer, self.fleet_policy, preemptible=preemptible, now=_utc(now)
        )
        if assessment.rejection is None:
            return None
        return (
            f"{assessment.rejection.value}: cost={assessment.hourly_cost_micros} "
            f"limit={assessment.max_hourly_cost_micros} USD micros/hour"
        )

    def _available_fleet_machines(
        self,
        repository: ComputeUnitRepository,
        *,
        platform_fleet: bool,
        gpu: bool,
        current: ComputeUnitRecord | None,
    ) -> int | None:
        if not platform_fleet:
            return None
        usage = repository.platform_capacity_usage(gpu=gpu)
        headroom = max(self.fleet_policy.machine_limit(gpu=gpu) - usage, 0)
        return (
            current.desired_machines + current.stopped_machines if current is not None else 0
        ) + headroom

    def _running_cpu_exceeded(
        self, repository: ComputeUnitRepository, unit: ComputeUnitRecord, desired: int
    ) -> bool:
        """Whether running `desired` machines in this unit passes the fleet's vCPU cap.

        Read under the platform capacity lock, like the instance cap beside it.
        Only growth is refused; a unit already over the cap may keep what it runs.
        """
        if not unit.platform_fleet or unit.worker_gpu_count or desired <= unit.desired_machines:
            return False
        running = repository.platform_running_cpu_millicores()
        added = (desired - unit.desired_machines) * unit.worker_cpu_millicores
        return running + added > self.fleet_policy.max_running_cpu_millicores

    def _reserve_inventory(
        self, unit: ComputeUnitRecord, *, desired: int
    ) -> ProviderUnitSnapshot | None:
        """The provider's view of the unit's reserves, when growing it may resume one."""
        if not unit.stopped_machines or desired <= unit.desired_machines:
            return None
        provider, offer = self._resolved_internal_unit_provider(unit)
        if provider.pooled is None:
            return None
        return provider.pooled.describe_unit(self._provider_unit_request(unit, offer))

    @property
    def provider_machines(self) -> ProviderMachineReconciler:
        """Bound to this service's *current* configuration.

        `reclaim` and `scheduler_hooks` are reassigned after construction, so a
        reconciler captured once would answer from stale settings.
        """
        return ProviderMachineReconciler(
            context=self.context,
            reclaim=self.reclaim,
            pool_bootstrap_factory=self.pool_bootstrap_factory,
            workspace_changes=self.workspace_changes,
            scheduler_hooks=self.scheduler_hooks,
            source_cache_lifecycle=self.source_cache_lifecycle,
        )

    def record_provider_node_lifecycle(
        self,
        *,
        pool_id: str,
        provider_instance_id: str,
        lifecycle: MachineLifecycle,
        failure_reason: MachineBootstrapFailureReason | None,
        failure_detail: str = "",
        now: datetime | None = None,
    ) -> Machine:
        with self.context.database.session() as session:
            return self.record_provider_node_lifecycle_in_transaction(
                session,
                pool_id=pool_id,
                provider_instance_id=provider_instance_id,
                lifecycle=lifecycle,
                failure_reason=failure_reason,
                failure_detail=failure_detail,
                now=now,
            )

    def record_provider_node_lifecycle_in_transaction(
        self,
        session: DatabaseSession,
        *,
        pool_id: str,
        provider_instance_id: str,
        lifecycle: MachineLifecycle,
        failure_reason: MachineBootstrapFailureReason | None,
        failure_detail: str = "",
        now: datetime | None = None,
    ) -> Machine:
        """Move a provider node's machine along its lifecycle from what the node reports."""
        if lifecycle is MachineLifecycle.Failed and failure_reason is None:
            raise InvalidInputError("failed provider bootstrap requires a failure reason")
        if lifecycle is not MachineLifecycle.Failed and failure_reason is not None:
            raise InvalidInputError("provider bootstrap failure reason requires failed phase")
        current_time = _utc(now)
        repository = ComputeProviderInstanceRepository(session)
        record = repository.get_for_pool_instance(
            pool_id,
            provider_instance_id,
            for_update=True,
        )
        if record is None:
            raise NotFoundError("provider node is no longer active")
        pool = ComputeUnitRepository(session).get(pool_id)
        if pool is None:
            raise NotFoundError("provider node unit is gone")
        record, machine = self.provider_machines.machine_for_record(
            session, pool=pool, record=record, now=current_time
        )
        if (
            lifecycle is MachineLifecycle.Joining
            and machine.lifecycle in RETAINED_MACHINE_LIFECYCLES
            and record.status != ReservationStatus.Active.value
        ):
            # A reserve joins again when its stream authorizes the resume. Its
            # own report would admit the worker it runs before the stream decides.
            return machine
        if lifecycle is MachineLifecycle.Joining and machine.lifecycle is MachineLifecycle.Ready:
            # A restarted agent reports joining beside its first stream, whose
            # heartbeat may already have made the machine ready. The report is
            # for a failed or draining machine; a ready one stays ready.
            return machine
        updated = write_machine_lifecycle(
            session,
            machine,
            lifecycle,
            workspace_changes=self.workspace_changes,
            message=failure_detail,
            failure=failure_reason,
            now=current_time,
        )
        if lifecycle is MachineLifecycle.Joining and record.first_enrolled_at is None:
            # This survives deletion of the machine row and its foreign-key binding.
            repository.upsert(
                record.model_copy(
                    update={"first_enrolled_at": current_time, "updated_at": current_time}
                )
            )
        return updated

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
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(
                request.capacity_owner_id
            )
        if unit is None:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                desired_unit=desired_unit,
                reason="capacity owner is not managed by compute",
            )
        if unit.capacity_owner_kind is CapacityOwnerKind.PooledProvider:
            return self._acquire_pooled_capacity(unit, request, desired_unit=desired_unit)
        return _capacity_result(
            request,
            CapacityAcquisitionStatus.Unsupported,
            desired_unit=desired_unit,
            reason=f"capacity owner kind {unit.capacity_owner_kind.value!r} is unsupported",
        )

    def _plan_capacity_acquisition(
        self,
        request: CapacityAcquisitionRequest,
    ) -> CapacityAcquisitionResult:
        """Read authoritative capacity and plan the exact next bounded unit.

        Existing operation intent always wins so retries cannot advance capacity twice.
        """
        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(
                request.capacity_owner_id
            )
            operations = ComputeCapacityOperationRepository(session)
            operation = operations.get(
                request.capacity_owner_id,
                request.operation_id,
                for_update=True,
            )
            if operation is not None:
                _validate_capacity_operation_plan(operation, request)
                if (
                    operation.status is CapacityOperationStatus.AtLimit
                    and not operation.owns_capacity
                ):
                    operation = operations.upsert(
                        operation.model_copy(
                            update={
                                "status": CapacityOperationStatus.Released,
                                "updated_at": utc_now(),
                            }
                        )
                    )
            launching = [
                item
                for item in ComputeCapacityOperationRepository(session).list_open_for_owner(
                    request.capacity_owner_id
                )
                if item.status is CapacityOperationStatus.Intent and item.owns_capacity
            ]
            direct_units = (
                len(
                    [
                        record
                        for record in ComputeProviderInstanceRepository(session).list_for_pool(
                            unit.id
                        )
                        if _reservation_open(record.status)
                    ]
                )
                if unit is not None
                else 0
            )
        if unit is None:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                desired_unit=1,
                reason="capacity owner is not managed by compute",
            )
        if operation is not None:
            status = _stored_capacity_status(operation.status)
            if status is CapacityAcquisitionStatus.TemporarilyUnavailable:
                status = CapacityAcquisitionStatus.Requested
            return _operation_result(operation, status)
        degraded_reason = unit.provider_state.degraded_reason
        if degraded_reason is not None:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                desired_unit=max(direct_units, 1),
                reason=degraded_reason,
            )
        if not unit.scaling_enabled:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                desired_unit=max(direct_units, 1),
                reason="capacity owner scaling is disabled",
            )
        if not _shape_matches_pool(request.shape, unit):
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                desired_unit=max(direct_units, 1),
                reason="requested unit does not match the capacity owner's fixed worker shape",
            )
        if unit.capacity_owner_kind is CapacityOwnerKind.PooledProvider:
            return _plan_next_capacity_unit(
                request,
                current_units=min(
                    (item.previous_desired_unit for item in launching),
                    default=unit.desired_machines,
                ),
                max_units=None,
            )
        return _capacity_result(
            request,
            CapacityAcquisitionStatus.Unsupported,
            desired_unit=1,
            reason=f"capacity owner kind {unit.capacity_owner_kind.value!r} is unsupported",
        )

    def fulfill_acquired_capacity(
        self, request: CapacityFulfillmentRequest
    ) -> CapacityOperationStatus:
        with self.context.database.session() as session:
            repository = ComputeCapacityOperationRepository(session)
            operation = repository.get(
                request.capacity_owner_id, request.operation_id, for_update=True
            )
            if operation is None or operation.reservation_id != request.reservation_id:
                raise ConflictError("capacity fulfillment does not own this operation")
            if operation.status is CapacityOperationStatus.Fulfilled:
                if operation.target_machine_id != request.machine_id:
                    raise ConflictError("capacity operation already fulfilled by another machine")
                return operation.status
            if operation.status.terminal or operation.status is CapacityOperationStatus.Releasing:
                return operation.status
            machine = ComputeProviderInstanceRepository(session).get_by_machine(request.machine_id)
            if machine is None or machine.pool_id != operation.pool_id:
                raise ConflictError("capacity fulfillment machine belongs to another pool")
            enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                operation.workspace_id, request.machine_id
            )
            if enrollment is None or enrollment.capacity_owner_id != operation.capacity_owner_id:
                raise ConflictError("capacity fulfillment requires an enrolled machine")
            repository.upsert(
                operation.model_copy(
                    update={
                        "status": CapacityOperationStatus.Fulfilled,
                        "target_machine_id": request.machine_id,
                        "provider_instance_id": machine.id,
                        "owns_capacity": False,
                        "fulfilled_at": utc_now(),
                        "updated_at": utc_now(),
                    }
                )
            )
            return CapacityOperationStatus.Fulfilled

    def release_acquired_capacity(
        self,
        request: CapacityReleaseRequest,
    ) -> CapacityAcquisitionResult:
        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(
                request.capacity_owner_id
            )
            operation = ComputeCapacityOperationRepository(session).get(
                request.capacity_owner_id,
                request.operation_id,
            )
        if unit is None or operation is None or operation.reservation_id != request.reservation_id:
            return CapacityAcquisitionResult(
                status=CapacityAcquisitionStatus.Unsupported,
                capacity_owner_id=request.capacity_owner_id,
                reservation_id=request.reservation_id,
                desired_unit=max(operation.desired_unit if operation is not None else 1, 1),
                reason="capacity operation is not owned by this reservation",
            )
        if operation.status.terminal:
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
                        current.model_copy(
                            update={
                                "status": CapacityOperationStatus.Released,
                                "updated_at": utc_now(),
                            }
                        )
                    )
            return _operation_result(operation, CapacityAcquisitionStatus.Requested)
        if unit.capacity_owner_kind is CapacityOwnerKind.PooledProvider:
            return self._release_pooled_capacity(unit, operation)
        return _operation_result(
            operation,
            CapacityAcquisitionStatus.Unsupported,
            reason="capacity owner does not support compute release",
        )

    def _acquire_pooled_capacity(
        self,
        pool: ComputeUnitRecord,
        request: CapacityAcquisitionRequest,
        *,
        desired_unit: int,
    ) -> CapacityAcquisitionResult:
        try:
            current_pool, provider, offer = self._internal_unit_provider(
                pool.workspace_id,
                pool.capacity_owner_id,
            )
        except (KeyError, RuntimeError, ValueError, UpstreamUnavailableError) as exc:
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
        if provider.policy is None:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason="capacity provider has no acquisition policy",
                desired_unit=desired_unit,
            )
        try:
            snapshot = provider.pooled.describe_unit(
                self._provider_unit_request(current_pool, offer)
            )
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
        requested_provider_units, _ = provider_unit_operational_capacity(
            current_pool.model_copy(update={"desired_machines": desired_unit})
        )
        preemptible = (
            request.shape.preemptible
            if request.workload_preemptible is None
            else request.workload_preemptible
        )
        if snapshot.desired_machines < requested_provider_units:
            try:
                offer = self._available_unit_offer(provider, current_pool)
            except (KeyError, RuntimeError, ValueError, UpstreamUnavailableError) as exc:
                return _capacity_result(
                    request,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason=capacity_failure_message(
                        CapacityFailureCode.CapacityPlanningFailed,
                        exception_type=type(exc).__name__,
                    ),
                    failure_code=CapacityFailureCode.CapacityPlanningFailed,
                    desired_unit=desired_unit,
                )
            if rejection := self.pooled_offer_rejection(provider, offer, preemptible=preemptible):
                return _capacity_result(
                    request,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason=rejection,
                    desired_unit=desired_unit,
                )
        if not _offer_matches_capacity_shape(offer, request.shape):
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                reason="requested unit does not match the capacity owner's fixed worker shape",
                desired_unit=desired_unit,
            )
        with self.context.database.session() as session:
            pools = ComputeUnitRepository(session)
            assert provider.policy is not None
            if provider.policy.platform_fleet:
                pools.lock_platform_capacity()
            else:
                pools.lock_capacity_workspace(provider.policy.workspace_id)
            locked_pool = pools.get(current_pool.id, for_update=True)
            if locked_pool is None:
                return _capacity_result(
                    request,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason="capacity owner disappeared during acquisition",
                    desired_unit=desired_unit,
                )
            if locked_pool.phase in ENDED_UNIT_PHASES:
                return _capacity_result(
                    request,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason="capacity owner must finish retirement before it can be prepared again",
                    desired_unit=desired_unit,
                )
            intent_pool = locked_pool
            operations = ComputeCapacityOperationRepository(session)
            operation = operations.get(
                request.capacity_owner_id,
                request.operation_id,
                for_update=True,
            )
            if operation is not None:
                _validate_capacity_operation(operation, request, desired_unit=desired_unit)
                if operation.status.terminal:
                    return _operation_result(
                        operation,
                        CapacityAcquisitionStatus.Unsupported,
                        reason="released capacity operation cannot be reacquired",
                    )
                if operation.status == CapacityAcquisitionStatus.Rejected.value:
                    return _operation_result(operation, CapacityAcquisitionStatus.Rejected)
                if (
                    snapshot.last_capacity_failure_at is not None
                    and to_utc(snapshot.last_capacity_failure_at) >= to_utc(operation.created_at)
                    and snapshot.observed_machines < requested_provider_units
                ):
                    failure_code = snapshot.last_capacity_failure_code
                    reason = capacity_failure_message(failure_code)
                    operations.upsert(
                        operation.model_copy(
                            update={
                                "status": CapacityOperationStatus.Rejected,
                                "last_error": reason,
                                "failure_code": failure_code,
                                "updated_at": utc_now(),
                            }
                        )
                    )
                    pools.apply_provider_state(
                        locked_pool.id,
                        generation=locked_pool.generation,
                        observed_machines=locked_pool.observed_machines,
                        phase=ComputeUnitPhase.Degraded,
                        provider_state=locked_pool.provider_state.model_copy(
                            update={
                                "degraded_reason": "provider_acquisition_rejected",
                                "degraded_at": utc_now(),
                            }
                        ),
                    )
                    return _operation_result(
                        operation,
                        CapacityAcquisitionStatus.Rejected,
                        reason=reason,
                        failure_code=failure_code,
                    )
                if not operation.owns_capacity:
                    return _operation_result(
                        operation,
                        _stored_capacity_status(operation.status),
                    )
            else:
                current_units = locked_pool.desired_machines
                available = self._available_fleet_machines(
                    pools,
                    platform_fleet=locked_pool.platform_fleet,
                    gpu=_pool_gpu_capacity(locked_pool),
                    current=locked_pool,
                )
                if (
                    available is not None
                    and desired_unit > available
                    and desired_unit > current_units
                ):
                    return _capacity_result(
                        request,
                        CapacityAcquisitionStatus.AtLimit,
                        desired_unit=desired_unit,
                        reason="fleet capacity limit reached",
                    )
                if self._running_cpu_exceeded(pools, locked_pool, desired_unit):
                    return _capacity_result(
                        request,
                        CapacityAcquisitionStatus.AtLimit,
                        desired_unit=desired_unit,
                        reason="fleet running vCPU limit reached",
                    )
                if desired_unit <= current_units:
                    operation = operations.upsert(
                        _new_capacity_operation(
                            locked_pool,
                            request,
                            desired_unit=desired_unit,
                            status=CapacityOperationStatus.ExistingPending,
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
                            status=CapacityOperationStatus.TemporarilyUnavailable,
                            previous_desired_unit=current_units,
                            last_error="desired unit skips authoritative pooled capacity",
                        )
                    )
                    return _operation_result(
                        operation,
                        CapacityAcquisitionStatus.TemporarilyUnavailable,
                        reason=operation.last_error,
                    )
                resumed = _reserves_resumed(snapshot, locked_pool, desired=desired_unit)
                if (
                    resumed
                    and preemptible
                    and locked_pool.id
                    in reserve_admission(
                        pools.stopped_reserve_units(), self.fleet_policy
                    ).withheld_from_preemptible
                ):
                    resumed = 0
                committed = desired_unit + locked_pool.stopped_machines - resumed
                maximum = (
                    max(locked_pool.max_machines, committed, 1)
                    if available is None
                    else max(available, committed, 1)
                )
                intent_pool = pools.update_capacity(
                    locked_pool.id,
                    expected_generation=locked_pool.generation,
                    desired_machines=desired_unit,
                    max_machines=maximum,
                    observed_machines=locked_pool.observed_machines,
                    phase=ComputeUnitPhase.Updating,
                    provider_state=locked_pool.provider_state,
                    stopped_machines=locked_pool.stopped_machines - resumed,
                )
                if intent_pool is None:
                    return _capacity_result(
                        request,
                        CapacityAcquisitionStatus.TemporarilyUnavailable,
                        reason="capacity owner intent was superseded",
                        desired_unit=desired_unit,
                    )
                operation = operations.upsert(
                    _new_capacity_operation(
                        locked_pool,
                        request,
                        desired_unit=desired_unit,
                        status=CapacityOperationStatus.Intent,
                        previous_desired_unit=current_units,
                        owns_capacity=True,
                    )
                )
            current_pool = intent_pool if operation.owns_capacity else locked_pool
            if snapshot.desired_machines < requested_provider_units:
                current_pool = pools.upsert(record_purchase_terms(current_pool, offer))
        if snapshot.desired_machines >= requested_provider_units:
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
                                "status": CapacityOperationStatus.Requested,
                                "last_error": "",
                                "failure_count": 0,
                                "updated_at": utc_now(),
                            }
                        )
                    )
            return _operation_result(operation, CapacityAcquisitionStatus.ExistingPending)
        try:
            provider_request = self._provider_unit_request(current_pool, offer)
            updated_snapshot = provider.pooled.set_unit_capacity(
                provider_request,
                desired_machines=provider_request.desired_machines,
                max_machines=provider_request.max_machines,
            )
            self.provider_machines._apply_pooled_snapshot(
                current_pool,
                offer,
                updated_snapshot,
                provider=provider.pooled,
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
                        "status": CapacityOperationStatus.Requested,
                        "last_error": "",
                        "failure_count": 0,
                        "updated_at": utc_now(),
                    }
                )
            )
        return _operation_result(operation, CapacityAcquisitionStatus.Requested)

    def _release_pooled_capacity(
        self,
        pool: ComputeUnitRecord,
        operation: ComputeCapacityOperationRecord,
    ) -> CapacityAcquisitionResult:
        try:
            current_pool, provider, offer = self._internal_unit_provider(
                pool.workspace_id, pool.capacity_owner_id
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            return _operation_result(
                operation,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason=capacity_failure_message(
                    CapacityFailureCode.Unknown, exception_type=type(exc).__name__
                ),
                failure_code=CapacityFailureCode.Unknown,
            )
        if provider.pooled is None:
            return _operation_result(
                operation,
                CapacityAcquisitionStatus.Unsupported,
                reason="capacity owner is not backed by a pooled provider",
            )
        with self.context.database.session() as session:
            pools = ComputeUnitRepository(session)
            locked_pool = pools.get(current_pool.id, for_update=True)
            if locked_pool is None:
                return _operation_result(
                    operation,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                    reason="capacity owner disappeared during release",
                )
            operations = ComputeCapacityOperationRepository(session)
            current = operations.get(
                operation.capacity_owner_id, operation.operation_id, for_update=True
            )
            if current is None:
                return _operation_result(
                    operation,
                    CapacityAcquisitionStatus.Unsupported,
                    reason="capacity operation disappeared during release",
                )
            if current.status.terminal:
                return _operation_result(current, CapacityAcquisitionStatus.Requested)
            if current.release_desired_unit is None:
                target = max(locked_pool.min_machines, locked_pool.desired_machines - 1)
                intent = pools.update_capacity(
                    locked_pool.id,
                    expected_generation=locked_pool.generation,
                    desired_machines=target,
                    max_machines=locked_pool.max_machines,
                    observed_machines=locked_pool.observed_machines,
                    phase=ComputeUnitPhase.Updating,
                    provider_state=locked_pool.provider_state,
                )
                if intent is None:
                    raise ConflictError("compute capacity release intent was superseded")
                locked_pool = intent
                current = operations.upsert(
                    current.model_copy(
                        update={
                            "status": CapacityOperationStatus.Releasing,
                            "release_desired_unit": target,
                            "updated_at": utc_now(),
                        }
                    )
                )
            current_pool = locked_pool
        provider_request = self._provider_unit_request(current_pool, offer)
        try:
            snapshot = provider.pooled.set_unit_capacity(
                provider_request,
                desired_machines=provider_request.desired_machines,
                max_machines=provider_request.max_machines,
            )
            self.provider_machines._apply_pooled_snapshot(
                current_pool,
                offer,
                snapshot,
                provider=provider.pooled,
            )
        except Exception as exc:
            return _operation_result(
                current,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason=capacity_failure_message(
                    CapacityFailureCode.ProviderUnavailable, exception_type=type(exc).__name__
                ),
                failure_code=CapacityFailureCode.ProviderUnavailable,
            )
        if snapshot.desired_machines != provider_request.desired_machines:
            return _operation_result(
                current,
                CapacityAcquisitionStatus.TemporarilyUnavailable,
                reason="provider has not confirmed pooled capacity intent",
            )
        # Releasing the allocation lowers desired capacity. Named idle retirement
        # removes surplus machines after their work finishes.
        with self.context.database.session() as session:
            operations = ComputeCapacityOperationRepository(session)
            locked = operations.get(
                operation.capacity_owner_id, operation.operation_id, for_update=True
            )
            if locked is None:
                raise RuntimeError("pooled capacity operation disappeared after release")
            current = operations.upsert(
                locked.model_copy(
                    update={
                        "status": CapacityOperationStatus.Released,
                        "owns_capacity": False,
                        "last_error": "",
                        "updated_at": utc_now(),
                    }
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
                            "status": CapacityOperationStatus.TemporarilyUnavailable,
                            "last_error": reason,
                            "failure_code": failure_code,
                            # The count is what the sizer's exponential backoff is
                            # computed from after a restart; the scheduler holds
                            # no copy of it.
                            "failure_count": operation.failure_count + 1,
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

    def create_unit(
        self,
        name: UnitName,
        *,
        workspace: str = "default",
        placement: Placement | None = None,
        provider: str = "local",
        capacity_owner_id: str | None = None,
        initial_machines: int = 0,
        min_machines: int = 0,
        max_machines: int = 1,
        scaling_enabled: bool = False,
        priority: int = 0,
        min_free_cpu_millicores: int = 0,
        min_free_memory_mib: int = 0,
        min_free_gpu_count: int = 0,
        worker_cpu_millicores: int = 0,
        worker_memory_mib: int = 0,
        worker_gpu_type: str = "",
        worker_gpu_count: int = 0,
        worker_runtimes: tuple[str, ...] = (OciRuntimeName.Runsc.value,),
        worker_preemptible: bool = False,
        idle_drain_timeout_seconds: int = 300,
        scale_up_cooldown_seconds: int = 5,
        scale_down_cooldown_seconds: int = 60,
        registration_timeout_seconds: int = 600,
        fallback: PrivateUnitFallback = PrivateUnitFallback.Internal,
    ) -> ComputeUnitRecord:
        """Create or update a provisioning unit the workspace owns directly.

        Without a placement the unit is platform capacity.
        """
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = ComputeUnitRepository(session)
            existing = repository.get_by_name(workspace_id, name, for_update=True)
            if existing is not None and existing.provider != provider:
                raise ConflictError(f"compute pool provider is immutable: {name}")
            if (
                existing is not None
                and capacity_owner_id is not None
                and existing.capacity_owner_id != capacity_owner_id
            ):
                raise ConflictError(f"compute pool capacity owner is immutable: {name}")
            owner_kind, owner_source = capacity_owner_for_provider(provider)
            owner = capacity_owner_id or str(uuid4())
            saved = repository.upsert(
                ComputeUnitRecord(
                    id=existing.id if existing is not None else owner,
                    capacity_owner_id=(
                        existing.capacity_owner_id if existing is not None else owner
                    ),
                    capacity_owner_kind=(
                        existing.capacity_owner_kind if existing is not None else owner_kind
                    ),
                    capacity_owner_source=(
                        existing.capacity_owner_source if existing is not None else owner_source
                    ),
                    workspace_id=workspace_id,
                    name=name,
                    placement=placement or Placement.platform(),
                    provider=provider,
                    selector=name,
                    status=ComputeUnitPhase.Ready.value,
                    source="workspace",
                    initial_machines=initial_machines,
                    desired_machines=(
                        existing.desired_machines if existing is not None else min_machines
                    ),
                    min_machines=min_machines,
                    max_machines=max_machines,
                    observed_machines=(existing.observed_machines if existing is not None else 0),
                    generation=existing.generation if existing is not None else 1,
                    phase=ComputeUnitPhase.Ready,
                    scaling_enabled=scaling_enabled,
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
                    fallback=fallback,
                )
            )
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=(
                WorkspaceChangeType.Created if existing is None else WorkspaceChangeType.Updated
            ),
            resource_id=saved.id,
        )
        return saved

    def list_units(self, *, workspace: str = "default") -> list[ComputeUnitRecord]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = ComputeUnitRepository(session).list_for_workspace(
                workspace_id, include_retired_platform=False
            )
        return sorted(records, key=lambda item: item.name)

    def list_units_across_workspaces(
        self, *, capacity_owner_kind: CapacityOwnerKind | None = None
    ) -> list[ComputeUnitRecord]:
        """Current provisioning units for scheduler controller construction.

        A unit carries its own workspace, so the caller does not pair it with
        one; two units in the same group are distinguished by capacity owner.
        """
        with self.context.database.session() as session:
            records = ComputeUnitRepository(session).list_across_workspaces(
                capacity_owner_kind=capacity_owner_kind
            )
        return sorted(records, key=lambda item: (item.workspace_id, item.name))

    def pool_sizing_snapshot(self, capacity_owner_id: str) -> CapacityPoolSizingSnapshot:
        """Answer how big this pool is, was asked to be, and how badly that went.

        Every field is derived here rather than stored, from the provider's own
        desired count, the pool's capacity operation rows, and its machine
        records. A pending operation is one compute committed to but has not
        confirmed with the provider; the caller must re-drive that exact
        operation id rather than open a new one, because the provider
        idempotency key is derived from it.

        Capacity leaving the pool is read from the machine records rather than
        the operation rows, because the two paths that retire a machine —
        `release_internal_pool_machine` and `terminate_pool_machine` — act on
        those records directly and write no operation row at all. Anchoring the
        scale-down cooldown on the operations would silently lose every
        drain-initiated release.
        """
        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).sizing_for_owner(capacity_owner_id)
            if unit is None:
                raise ConflictError(
                    f"compute pool capacity owner does not exist: {capacity_owner_id}"
                )
            operations = ComputeCapacityOperationRepository(session)
            open_operations = operations.list_open_sizing_for_owner(capacity_owner_id)
            operation_history = operations.sizing_history_summary_for_owner(capacity_owner_id)
            machines = ComputeProviderInstanceRepository(session).sizing_summary_for_pool(
                unit.id,
                terminal_statuses=(ReservationStatus.Deleted.value, ReservationStatus.Failed.value),
            )
        desired_units = (
            unit.desired_machines
            if unit.capacity_owner_kind is CapacityOwnerKind.PooledProvider
            else machines.open_count
        )
        pending = next(
            (
                operation
                for operation in reversed(open_operations)
                if operation.owns_capacity and operation.status in _UNCONFIRMED_OPERATION_STATUSES
            ),
            None,
        )
        failed = (
            pending
            if pending is not None
            and pending.status == CapacityAcquisitionStatus.TemporarilyUnavailable.value
            else None
        )
        return CapacityPoolSizingSnapshot(
            capacity_owner_id=capacity_owner_id,
            desired_units=desired_units,
            peak_desired_units=operation_history.peak_desired_unit,
            pending_operation_id=pending.operation_id if pending is not None else "",
            pending_desired_units=pending.desired_unit if pending is not None else 0,
            last_requested_at=operation_history.last_requested_at,
            last_released_at=machines.last_released_at,
            consecutive_failures=max(failed.failure_count, 1) if failed is not None else 0,
            last_failure_at=failed.updated_at if failed is not None else None,
        )

    def list_pools_for_workspace_deletion(self, workspace_id: str) -> list[ComputeUnitRecord]:
        with self.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            records = ComputeUnitRepository(session).list_for_workspace(workspace_id)
        records.sort(key=lambda item: item.name)
        return records

    def platform_units(self) -> list[ComputeUnitRecord]:
        with self.context.database.session() as session:
            namespace = WorkspaceRepository(session).platform()
            if namespace is None:
                raise NotFoundError("platform namespace is not initialized")
            return ComputeUnitRepository(session).list_for_workspace(namespace.id)

    def delete_platform_unit(self, capacity_owner_id: str) -> None:
        leases = self._required_capacity_owner_mutations()
        with leases.mutation_lock(capacity_owner_id), leases.dispatch_lock(capacity_owner_id):
            with self.context.database.session() as session:
                namespace = WorkspaceRepository(session).platform()
                unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
                if unit is None:
                    return
                if (
                    namespace is None
                    or unit.workspace_id != namespace.id
                    or not unit.platform_fleet
                ):
                    raise InvalidInputError("capacity does not belong to this deployment")
                if self._machines_holding_active_work(session, pool_id=unit.id):
                    raise ConflictError("platform capacity still holds active work")
            if leases.has_open_reservations(capacity_owner_id):
                raise ConflictError("platform capacity still holds scheduling reservations")
            self.delete_unit(capacity_owner_id, workspace=namespace.id)

    def delete_unit(self, capacity_owner_id: str, *, workspace: str = "default") -> None:
        termination_errors: list[str] = []
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
        clients = self._provider_client_snapshot(workspace_id)
        with self.context.database.session() as session:
            compute_pool = ComputeUnitRepository(session).get_by_capacity_owner_id(
                capacity_owner_id
            )
            if compute_pool is not None:
                provider_instances = ComputeProviderInstanceRepository(session)
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if not _reservation_open(record.status):
                        continue
                    self.provider_machines._terminate_provider_record(
                        session,
                        record,
                        clients=clients,
                        reason="pool_deleted",
                        message="managed compute pool deleted",
                    )
        if compute_pool is not None and _owns_provider_pool_capacity(compute_pool):
            release_error = self._release_provider_pool_capacity(compute_pool)
            if release_error:
                termination_errors.append(release_error)
        with self.context.database.session() as session:
            compute_pool_repository = ComputeUnitRepository(session)
            compute_pool = (
                compute_pool_repository.get(compute_pool.id) if compute_pool is not None else None
            )
            if compute_pool is not None:
                provider_instances = ComputeProviderInstanceRepository(session)
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if not _reservation_open(record.status):
                        continue
                    detail = record.last_error or "provider unavailable"
                    termination_errors.append(f"{record.provider}/{record.id}: {detail}")
            if not termination_errors:
                machine_repository = MachineRepository(session)
                owner = compute_pool.capacity_owner_id if compute_pool is not None else ""
                for machine in machine_repository.list(workspace_id=workspace_id):
                    if (
                        machine.capacity_owner_id != owner
                        or machine.lifecycle is MachineLifecycle.Deleted
                    ):
                        continue
                    write_machine_lifecycle(
                        session,
                        machine,
                        MachineLifecycle.Deleted,
                        workspace_changes=self.workspace_changes,
                        workspace_id=workspace_id,
                        message="Unit deleted",
                    )
                unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
                # Pooled units retain terminal ownership and recovery history.
                if unit is not None and not _owns_provider_pool_capacity(unit):
                    ComputeUnitRepository(session).delete(
                        unit.id,
                        workspace_id=workspace_id,
                    )
        if termination_errors:
            details = "; ".join(termination_errors)
            raise UpstreamUnavailableError(
                f"managed compute pool capacity could not be terminated: {details}"
            )
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=WorkspaceChangeType.Deleted,
            resource_id=capacity_owner_id,
        )

    def _release_provider_pool_capacity(self, pool: ComputeUnitRecord) -> str:
        """Delete this pool's provider-side capacity, answering why it is still held.

        An empty answer proves provider resources are gone. The terminal unit
        remains as the owner of its machine and recovery history.

        The deleting intent is persisted before the provider is asked, because a
        failure after the provider call would otherwise leave a pool the
        reconciler restores to its previous size.
        """

        with self.context.database.session() as session:
            repository = ComputeUnitRepository(session)
            if pool.platform_fleet:
                repository.lock_platform_capacity()
            current = repository.get(pool.id, for_update=True)
            if current is None:
                return ""
            if (
                current.phase not in {ComputeUnitPhase.Deleting, ComputeUnitPhase.Deleted}
                or current.desired_machines
                or current.min_machines
                or current.stopped_machines
            ):
                current = repository.upsert(
                    current.model_copy(
                        update={
                            "desired_machines": 0,
                            "stopped_machines": 0,
                            "retiring_stopped_machines": current.retiring_stopped_machines
                            + current.stopped_machines,
                            "min_machines": 0,
                            "replacement_machine_id": "",
                            "replacement_template_version": "",
                            "generation": current.generation + 1,
                            "phase": ComputeUnitPhase.Deleting,
                            "status": ComputeUnitPhase.Deleting.value,
                        }
                    )
                )
        try:
            durable, provider, offer = self._internal_unit_provider(
                current.workspace_id,
                current.capacity_owner_id,
            )
            pooled = provider.pooled
            if pooled is None:
                raise RuntimeError(f"compute pool {current.name!r} provider is not pooled")
            released = self.provider_machines._apply_pooled_snapshot(
                durable,
                offer,
                pooled.delete_unit(self._provider_unit_request(durable, offer)),
                provider=pooled,
            )
        except Exception as exc:
            LOGGER.exception(
                "provider pool capacity release failed for pool %s (%s)",
                current.name,
                current.id,
            )
            return f"{current.provider_ref}/{current.name}: {exc}"
        if released.phase is not ComputeUnitPhase.Deleted:
            return (
                f"{current.provider_ref}/{current.name}: provider capacity release is "
                f"in progress ({released.phase.value})"
            )
        return ""

    def delete_unit_for_workspace_deletion(
        self, capacity_owner_id: str, *, workspace_id: str
    ) -> None:
        """Delete one existing pool without reopening a Deleting workspace."""
        termination_errors: list[str] = []
        clients = self._provider_client_snapshot_for_workspace_deletion(workspace_id)
        with self.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            compute_pool_repository = ComputeUnitRepository(session)
            compute_pool = compute_pool_repository.get_by_capacity_owner_id(capacity_owner_id)
            if compute_pool is not None:
                provider_instances = ComputeProviderInstanceRepository(session)
                for record in provider_instances.list_for_pool(compute_pool.id):
                    if not _reservation_open(record.status):
                        continue
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
                        detail = record.last_error or "provider unavailable"
                        termination_errors.append(f"{record.provider}/{record.id}: {detail}")
                if (
                    _owns_provider_pool_capacity(compute_pool)
                    and compute_pool.phase is not ComputeUnitPhase.Deleted
                ):
                    # Workspace deletion only begins once the provider account is
                    # disconnected, so nothing here can still reach the provider to
                    # release a pool. The deleted phase the drain recorded is the
                    # only proof the provider holds nothing, and dropping the row
                    # without it orphans an Auto Scaling group no record can name.
                    termination_errors.append(
                        f"{compute_pool.provider_ref}/{compute_pool.name}: provider pool "
                        f"capacity is still held ({compute_pool.phase.value})"
                    )
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
            owner = compute_pool.capacity_owner_id if compute_pool is not None else ""
            for machine in machine_repository.list(workspace_id=workspace_id):
                if (
                    machine.capacity_owner_id != owner
                    or machine.lifecycle is MachineLifecycle.Deleted
                ):
                    continue
                machine_repository.mark_deleted_for_workspace_deletion(
                    machine.id,
                    workspace_id=workspace_id,
                )
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
            if unit is not None:
                ComputeUnitRepository(session).delete_for_workspace_deletion(
                    unit.id,
                    workspace_id=workspace_id,
                )

    def pooled_providers(self, workspace_id: str) -> tuple[ResolvedComputeProvider, ...]:
        if self.provider_resolver is None:
            return ()
        return tuple(
            provider
            for provider in self.provider_resolver.list_providers(workspace_id)
            if provider.capacity_mode is ComputeCapacityMode.Pooled
        )

    def workspace_has_ready_customer_connection(self, workspace: str) -> bool:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(
                workspace_id
            )
        return connection is not None and connection.hosts_workloads

    def reserve_admission(self) -> ReserveAdmission:
        with self.context.database.session() as session:
            rows = ComputeUnitRepository(session).stopped_reserve_units()
        return reserve_admission(rows, self.fleet_policy)

    def reported_node_memory(self) -> dict[tuple[int, int, int], int]:
        with self.context.database.session() as session:
            return ComputeUnitRepository(session).reported_node_memory()

    def pooled_offer_owner_id(self, provider: ResolvedComputeProvider, offer: ComputeOffer) -> str:
        return self.pooled_offer_owners(provider, [offer])[offer.id]

    def pooled_offer_owners(
        self, provider: ResolvedComputeProvider, offers: list[ComputeOffer]
    ) -> dict[str, str]:
        """The unit that owns, or would own, each offer."""
        policy = provider.policy
        if policy is None or provider.pooled is None:
            raise InvalidInputError("offer does not belong to a pooled provider")
        identities: dict[str, tuple[str, str, str, str, int]] = {}
        for offer in offers:
            if offer.provider != provider.ref:
                raise InvalidInputError("offer does not belong to a pooled provider")
            identities[offer.id] = (
                policy.workspace_id,
                provider.ref,
                offer.region,
                offer.capability_key,
                policy.root_volume_gib,
            )
        if not identities:
            return {}
        with self.context.database.session() as session:
            states = ComputeUnitRepository(session).offer_states(tuple(set(identities.values())))
        return {
            offer_id: state.id
            if (state := states.get(identity)) is not None
            else internal_unit_identity(
                workspace_id=identity[0],
                provider_ref=identity[1],
                region=identity[2],
                capability_key=identity[3],
                root_volume_gib=identity[4],
            )[0]
            for offer_id, identity in identities.items()
        }

    def prepare_pooled_offer(
        self,
        *,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        requirements: ComputeResourceRequirements,
    ) -> ComputeUnitRecord:
        policy = provider.policy
        if policy is None or provider.pooled is None or offer.provider != provider.ref:
            raise InvalidInputError("offer does not belong to a pooled provider")
        if rejection := self.pooled_offer_rejection(
            provider, offer, preemptible=requirements.preemptible
        ):
            raise InvalidInputError(rejection)
        if policy.platform_fleet:
            gpu = requirements.gpu_count > 0
            with self.context.database.session() as session:
                units = ComputeUnitRepository(session)
                units.lock_platform_capacity()
                if units.platform_capacity_usage(gpu=gpu) >= self.fleet_policy.machine_limit(
                    gpu=gpu
                ):
                    self._retire_incompatible_reserves(
                        session,
                        gpu=gpu,
                        hosts=lambda unit: (
                            node_fits_request(
                                unit.worker_cpu_millicores,
                                unit.worker_memory_mib,
                                cpu_millicores=requirements.cpu_millicores,
                                memory_mib=requirements.memory_mb,
                            )
                            and unit.worker_gpu_count >= requirements.gpu_count
                            and (
                                not gpu
                                or gpu_preference_accepts(requirements.gpu, unit.worker_gpu_type)
                            )
                            and (
                                not requirements.runtime
                                or requirements.runtime in unit.worker_runtimes
                            )
                            and (requirements.preemptible or not unit.worker_preemptible)
                        ),
                    )
        return self._prepare_pooled_offer(
            provider=provider,
            offer=offer,
            requirements=requirements,
            desired_machines=0,
            root_volume_gib=policy.root_volume_gib,
            idle_timeout_seconds=policy.idle_timeout_seconds,
            baseline=None,
        )

    @staticmethod
    def _retire_incompatible_reserves(
        session: DatabaseSession,
        *,
        gpu: bool,
        hosts: Callable[[ComputeUnitRecord], bool],
    ) -> None:
        """At the fleet limit, give waiting work the slots held by reserves it cannot use."""
        demand = ContainerRepository(session).unplaced_platform_demand()
        if not (demand.gpu_types if gpu else demand.cpu):
            return
        units = ComputeUnitRepository(session)
        for unit in units.list_platform_internal(gpu=gpu):
            if not unit.stopped_machines or hosts(unit):
                continue
            units.upsert(
                unit.model_copy(
                    update={
                        "stopped_machines": 0,
                        "retiring_stopped_machines": unit.retiring_stopped_machines
                        + unit.stopped_machines,
                        "generation": unit.generation + 1,
                    }
                )
            )

    def reconcile_aws_default_capacity(
        self,
        *,
        workspace: str,
        region: str,
        instance_type: str,
        initial_machines: int,
        min_machines: int,
        min_free_cpu_millicores: int,
        min_free_memory_mib: int,
        root_volume_gib: int,
        idle_timeout_seconds: int,
    ) -> ComputeUnitRecord:
        """Reconcile the permanent CPU floor for an AWS-default workspace."""
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(
                workspace_id
            )
        if connection is None:
            raise UpstreamUnavailableError("AWS baseline connection is unavailable")
        pool = self.prepare_pooled_capacity(
            workspace=workspace,
            requirements=ComputeResourceRequirements(),
            region=region,
            desired_machines=initial_machines,
            root_volume_gib=root_volume_gib,
            idle_timeout_seconds=idle_timeout_seconds,
            allowed_instance_types=(instance_type,),
            provider_ref=f"aws:{connection.id}",
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
        # preparing session has closed; `scale_internal_unit` takes the mutation
        # lease and locks the same row.
        return self.scale_internal_unit(
            pool.workspace_id,
            pool.capacity_owner_id,
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
            connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(
                workspace_id
            )
            if connection is None:
                return
            provider_ref = f"aws:{connection.id}"
            self._clear_other_internal_pool_floors(
                session,
                workspace_id=workspace_id,
                keep_pool_id=None,
                provider_ref=provider_ref,
            )
        if not release_capacity:
            return
        with self.context.database.session() as session:
            pools = [
                unit
                for unit in ComputeUnitRepository(session).list_internal(workspace_id=workspace_id)
                if unit.provider_ref == provider_ref
            ]
            protected = {
                pool.id: self._machines_holding_active_work(
                    session,
                    pool_id=pool.id,
                )
                for pool in pools
            }
        for pool in pools:
            if pool.phase in {ComputeUnitPhase.Deleting, ComputeUnitPhase.Deleted}:
                continue
            floor = protected[pool.id]
            if pool.desired_machines <= floor:
                continue
            self.scale_internal_unit(
                workspace_id,
                pool.capacity_owner_id,
                floor,
                before_mutation=_policy_owned_scale,
            )

    def _machines_holding_active_work(
        self,
        session: DatabaseSession,
        *,
        pool_id: str,
    ) -> int:
        """Count machines with pending or running work across all tenant workspaces."""

        machine_ids = {
            instance.machine_id
            for instance in ComputeProviderInstanceRepository(session).list_for_pool(pool_id)
            if instance.machine_id
        }
        if not machine_ids:
            return 0
        containers = ContainerRepository(session)
        return sum(containers.count_live_for_machine(machine_id) > 0 for machine_id in machine_ids)

    def prepare_pooled_capacity(
        self,
        *,
        workspace: str,
        requirements: ComputeResourceRequirements,
        region: str,
        desired_machines: int,
        root_volume_gib: int,
        idle_timeout_seconds: int = 300,
        allowed_instance_types: tuple[str, ...] = (),
        baseline: _PooledCapacityBaseline | None = None,
        provider_ref: str = "",
    ) -> ComputeUnitRecord:
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
            if provider.capacity_mode is ComputeCapacityMode.Pooled
            and provider.pooled is not None
            and (not provider_ref or provider.ref == provider_ref)
        ]
        if not providers:
            raise ManagedComputeLaunchError(
                "workspace has no ready pooled compute provider",
                code="provider_unavailable",
            )
        offers: list[ComputeOffer] = []
        for provider in providers:
            pooled = provider.pooled
            if pooled is None or provider.policy is None or not provider.policy.can_purchase:
                continue
            offers.extend(
                offer
                for offer in pooled.list_offers(root_volume_gib=root_volume_gib)
                if (not region or offer.region == region)
                and self.pooled_offer_rejection(
                    provider, offer, preemptible=requirements.preemptible
                )
                is None
                and (not allowed_instance_types or offer.instance_type in allowed_instance_types)
            )
        try:
            offer = choose_offer(
                offers,
                OfferRequest(
                    regions=[region] if region else [],
                    min_cpu_millicores=requirements.cpu_millicores,
                    min_memory_mb=requirements.memory_mb,
                    min_storage_mb=root_volume_gib * 1024,
                    architecture=requirements.architecture or "amd64",
                    preemptible=requirements.preemptible,
                    availability_zone=requirements.availability_zone,
                    runtime=requirements.runtime,
                    gpu=requirements.gpu,
                    min_gpu_count=requirements.gpu_count,
                    nodes=max(desired_machines, 1),
                ),
            )
        except ValueError as exc:
            raise ManagedComputeLaunchError(
                "no provider capacity matches the workload requirements and placement policy",
                code="offer_unavailable",
            ) from exc
        provider = next(item for item in providers if item.ref == offer.provider)
        return self._prepare_pooled_offer(
            provider=provider,
            offer=offer,
            requirements=requirements,
            desired_machines=desired_machines,
            root_volume_gib=root_volume_gib,
            idle_timeout_seconds=idle_timeout_seconds,
            baseline=baseline,
        )

    def _prepare_pooled_offer(
        self,
        *,
        provider: ResolvedComputeProvider,
        offer: ComputeOffer,
        requirements: ComputeResourceRequirements,
        desired_machines: int,
        root_volume_gib: int,
        idle_timeout_seconds: int,
        baseline: _PooledCapacityBaseline | None,
        now: datetime | None = None,
    ) -> ComputeUnitRecord:
        current_time = now or utc_now()
        if provider.policy is None or provider.pooled is None:
            raise ManagedComputeLaunchError(
                "pooled compute provider policy is unavailable",
                code="provider_unavailable",
            )
        workspace_id = provider.policy.workspace_id
        unit_id, unit_name = internal_unit_identity(
            workspace_id=workspace_id,
            provider_ref=provider.ref,
            region=offer.region,
            capability_key=offer.capability_key,
            root_volume_gib=root_volume_gib,
        )
        with self.context.database.session() as session:
            unit_pool = provider.policy.placement
            unit_platform_fleet = provider.policy.platform_fleet
            repository = ComputeUnitRepository(session)
            if unit_platform_fleet:
                repository.lock_platform_capacity()
            else:
                repository.lock_capacity_workspace(provider.policy.workspace_id)
            if self.provider_resolver is None:
                raise UpstreamUnavailableError("compute provider resolver is unavailable")
            provider = self.provider_resolver.resolve(workspace_id, provider.ref)
            if provider.policy is None:
                raise ConflictError("provider no longer accepts this capacity purchase")
            if rejection := self.pooled_offer_rejection(
                provider, offer, preemptible=requirements.preemptible
            ):
                raise ConflictError(
                    f"provider no longer accepts this capacity purchase: {rejection}"
                )
            current = repository.get_by_identity(
                workspace_id=workspace_id,
                provider_ref=provider.ref,
                region=offer.region,
                capability_key=offer.capability_key,
                root_volume_gib=root_volume_gib,
                for_update=True,
            )
            if current is not None:
                unit_id, unit_name = current.id, current.name
            if current is not None and current.phase is ComputeUnitPhase.Deleting:
                raise CapacityReservationConflictError(
                    f"compute pool {current.name!r} is finishing provider resource retirement"
                )
            if (
                current is not None
                and current.provider_state.degraded_reason is not None
                and not self._failed_market_retry_ready(current, now=current_time)
            ):
                raise UpstreamUnavailableError(
                    "capacity market is cooling down or still releasing failed capacity"
                )
            if current is not None and self._failed_market_retry_ready(current, now=current_time):
                records = ComputeProviderInstanceRepository(session).list_for_pool(current.id)
                if not ComputeCapacityOperationRepository(session).list_open_for_owner(
                    current.capacity_owner_id
                ) and not any(
                    _reservation_open(record.status)
                    or (
                        (record.instance_id is not None or record.storage_volume_ids)
                        and record.provider_storage_destroyed_at is None
                    )
                    for record in records
                ):
                    current = repository.upsert(
                        current.model_copy(
                            update={
                                "phase": (
                                    current.phase
                                    if current.phase is ComputeUnitPhase.Deleted
                                    else ComputeUnitPhase.Provisioning
                                ),
                                "status": (
                                    current.status
                                    if current.phase is ComputeUnitPhase.Deleted
                                    else ComputeUnitPhase.Provisioning.value
                                ),
                                "provider_state": current.provider_state.model_copy(
                                    update={
                                        "degraded_reason": None,
                                        "degraded_at": None,
                                        "launch_attempt_baseline": max(
                                            (record.launch_attempt for record in records), default=0
                                        ),
                                    }
                                ),
                            }
                        )
                    )
                else:
                    raise UpstreamUnavailableError(
                        "failed market capacity has not finished cleanup"
                    )
            remaining = self._available_fleet_machines(
                repository,
                platform_fleet=unit_platform_fleet,
                gpu=requirements.gpu_count > 0,
                current=current,
            )
            if remaining == 0 and (
                current is None
                or not (
                    current.desired_machines
                    or current.stopped_machines
                    or current.observed_machines
                )
            ):
                raise CapacityLimitReachedError(
                    "fleet capacity limit leaves no headroom for this capacity market"
                )
            requested_machines = max(
                desired_machines,
                baseline.initial_machines if baseline is not None else 0,
                baseline.min_machines if baseline is not None else 0,
            )
            if baseline is None:
                # Demand-driven placement never shrinks a pool it did not size.
                desired = max(
                    requested_machines,
                    current.desired_machines if current is not None else 0,
                )
                if remaining is not None:
                    desired = min(desired, remaining)
            else:
                desired = _policy_owned_desired_machines(
                    current_desired=current.desired_machines if current is not None else 0,
                    previous_floor=_previous_policy_floor(current),
                    floor=requested_machines,
                    ceiling=(
                        remaining
                        if remaining is not None
                        else max(
                            requested_machines,
                            current.desired_machines if current is not None else 0,
                        )
                    ),
                )
                if current is not None and desired < current.desired_machines:
                    # Release only down to the machines still running work; the
                    # drain owner takes the rest as they go idle.
                    desired = max(
                        desired,
                        min(
                            self._machines_holding_active_work(
                                session,
                                pool_id=current.id,
                            ),
                            current.desired_machines,
                        ),
                    )
                    if provider.policy.platform_fleet:
                        desired = current.desired_machines
            if requested_machines > 0 and desired < requested_machines:
                raise CapacityLimitReachedError(
                    f"fleet capacity limit reached: {requested_machines} machines "
                    f"requested, {desired} available"
                )
            maximum = (
                max(current.max_machines if current is not None else 0, desired, 1)
                if remaining is None
                else max(remaining, desired, 1)
            )
            minimum = (
                baseline.min_machines
                if baseline is not None
                else current.min_machines
                if current is not None
                else 0
            )
            # Only the baseline owns the durable floors. Demand-driven placement
            # reaches the same unit and must carry them through untouched, or the
            # warm capacity a workspace paid for is erased by the next request.
            initial = (
                baseline.initial_machines
                if baseline is not None
                else current.initial_machines
                if current is not None
                else 0
            )
            free_cpu = (
                baseline.min_free_cpu_millicores
                if baseline is not None
                else current.min_free_cpu_millicores
                if current is not None
                else 0
            )
            free_memory = (
                baseline.min_free_memory_mib
                if baseline is not None
                else current.min_free_memory_mib
                if current is not None
                else 0
            )
            free_gpu = current.min_free_gpu_count if current is not None else 0
            cost_terms = offer.cost_terms
            if current is not None and current.offer_cost_terms is not None:
                recorded_terms = current.offer_cost_terms
                if (
                    cost_terms.model_copy(update={"observed_at": recorded_terms.observed_at})
                    == recorded_terms
                ):
                    cost_terms = recorded_terms
            unit = ComputeUnitRecord(
                id=current.id if current is not None else unit_id,
                capacity_owner_id=current.capacity_owner_id if current is not None else unit_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                workspace_id=workspace_id,
                name=unit_name,
                placement=unit_pool,
                platform_fleet=unit_platform_fleet,
                provider=provider.ref,
                selector=unit_name,
                source="workspace_policy",
                provider_ref=provider.ref,
                provider_connection_id=provider.connection_id,
                capacity_mode=ComputeCapacityMode.Pooled,
                visibility=ComputeUnitVisibility.Internal,
                region=offer.region,
                offer_id=offer.id,
                capability_key=offer.capability_key,
                desired_machines=desired,
                stopped_machines=(
                    min(current.stopped_machines, max(maximum - desired, 0)) if current else 0
                ),
                retiring_stopped_machines=current.retiring_stopped_machines if current else 0,
                offer_cost_terms=cost_terms,
                offer_storage_mib=offer.storage_mb,
                offer_availability_zone=offer.availability_zone,
                supplier_cpu_unit=offer.supplier_cpu_unit,
                supplier_cpu_count=offer.supplier_cpu_count,
                initial_machines=min(max(initial, minimum), maximum),
                min_machines=minimum,
                max_machines=maximum,
                replacement_machine_id=(current.replacement_machine_id if current else ""),
                replacement_template_version=(
                    current.replacement_template_version if current else ""
                ),
                scaling_enabled=True,
                # True by construction rather than by preference: this unit is
                # built from workspace policy because the workspace needed general
                # capacity, so the work it serves is whatever that workspace runs.
                # A pool created by name through `create_unit` is the one that
                # earns an opt-in, and keeps the flag for it.
                worker_cpu_millicores=offer.cpu_millicores,
                worker_memory_mib=offer.memory_mb,
                worker_gpu_type=offer.gpu or "",
                worker_gpu_count=offer.gpu_count,
                worker_runtimes=(offer.runtime,),
                worker_preemptible=offer.preemptible,
                min_free_cpu_millicores=free_cpu,
                min_free_memory_mib=free_memory,
                min_free_gpu_count=free_gpu,
                idle_drain_timeout_seconds=idle_timeout_seconds,
                root_volume_gib=root_volume_gib,
                observed_machines=current.observed_machines if current is not None else 0,
                generation=(
                    current.generation + int(current.phase is ComputeUnitPhase.Deleted)
                    if current is not None
                    else 1
                ),
                # Only completed retirement can reactivate. The new generation
                # fences provider observations from the previous resource lifetime.
                phase=(
                    current.phase
                    if current is not None and current.phase not in ENDED_UNIT_PHASES
                    else ComputeUnitPhase.Provisioning
                ),
                status=(
                    current.status
                    if current is not None and current.phase not in ENDED_UNIT_PHASES
                    else ComputeUnitPhase.Provisioning.value
                ),
                provider_state=(
                    current.provider_state if current is not None else ComputeUnitProviderState()
                ),
                created_at=current.created_at if current is not None else utc_now(),
            )
            created = current is None
            if current is None or unit != current:
                current = repository.upsert(unit)
            if baseline is not None and not unit_platform_fleet:
                self._clear_other_internal_pool_floors(
                    session,
                    workspace_id=workspace_id,
                    keep_pool_id=current.id,
                    provider_ref=provider.ref,
                )
        if self.scheduler_hooks is not None:
            self.scheduler_hooks.register_internal_unit(current, offer)
        self._publish_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
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
        provider_ref: str,
    ) -> None:
        """Drop every durable floor the baseline no longer owns.

        A baseline that moves to a new capability leaves the unit it used to
        size still holding one; without this the workspace pays for both.
        """
        units = ComputeUnitRepository(session)
        for unit in units.list_internal(workspace_id=workspace_id):
            if unit.id == keep_pool_id or unit.provider_ref != provider_ref:
                continue
            cleared = _with_retained_machines(unit, 0)
            if cleared != unit:
                units.upsert(cleared)

    def _required_capacity_owner_mutations(self) -> CapacityOwnerMutationLease:
        if self.capacity_owner_mutations is None:
            raise UpstreamUnavailableError(
                "capacity-owner mutation lease service is not configured"
            )
        return self.capacity_owner_mutations

    def get_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> ComputeUnitRecord:
        """Read the durable pooled-provider intent for a workspace-owned pool."""

        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
        return _require_workspace_internal_pooled_unit(
            unit,
            workspace_id=workspace_id,
            unit_ref=capacity_owner_id,
        )

    def scale_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        desired_machines: int,
        *,
        before_mutation: Callable[[ComputeUnitRecord], None],
        now: datetime | None = None,
    ) -> ComputeUnitRecord:
        """Serialize, guard, persist, and apply one durable provider capacity intent."""

        if desired_machines < 0:
            raise InvalidInputError("desired compute pool capacity cannot be negative")
        initial = self.get_internal_unit(workspace_id, capacity_owner_id)
        mutations = self._required_capacity_owner_mutations()
        try:
            with mutations.mutation_lock(initial.capacity_owner_id):
                current = self.get_internal_unit(workspace_id, capacity_owner_id)
                if desired_machines >= current.desired_machines and desired_machines != 0:
                    return self._scale_internal_unit_under_lease(
                        workspace_id,
                        capacity_owner_id,
                        desired_machines,
                        before_mutation=before_mutation,
                        now=now,
                    )
                with mutations.dispatch_lock(initial.capacity_owner_id):
                    return self._scale_internal_unit_under_lease(
                        workspace_id,
                        capacity_owner_id,
                        desired_machines,
                        before_mutation=before_mutation,
                        now=now,
                    )
        except DomainError:
            raise
        except Exception as exc:
            raise UpstreamUnavailableError(
                "compute capacity-owner mutation lease is unavailable"
            ) from exc

    def _scale_internal_unit_under_lease(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        desired_machines: int,
        *,
        before_mutation: Callable[[ComputeUnitRecord], None],
        now: datetime | None,
    ) -> ComputeUnitRecord:
        current_time = _utc(now)
        verify_provider_zero = False
        purchase_offer: ComputeOffer | None = None
        reserves = self._reserve_inventory(
            self.get_internal_unit(workspace_id, capacity_owner_id), desired=desired_machines
        )
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            initial = _require_workspace_internal_pooled_unit(
                units.get_by_capacity_owner_id(capacity_owner_id),
                workspace_id=workspace_id,
                unit_ref=capacity_owner_id,
            )
            if self.provider_resolver is None:
                raise UpstreamUnavailableError("compute provider resolver is unavailable")
            provider = self.provider_resolver.resolve(workspace_id, initial.provider_ref)
            if provider.policy is None:
                raise UpstreamUnavailableError("compute provider policy is unavailable")
            if provider.policy.platform_fleet:
                units.lock_platform_capacity()
            else:
                units.lock_capacity_workspace(provider.policy.workspace_id)
            unit = _require_workspace_internal_pooled_unit(
                units.get_by_capacity_owner_id(capacity_owner_id, for_update=True),
                workspace_id=workspace_id,
                unit_ref=capacity_owner_id,
            )
            if unit.phase in ENDED_UNIT_PHASES:
                raise ConflictError("retired compute capacity must be prepared before scaling")
            before_mutation(unit)
            if desired_machines == 0 and self._machines_holding_active_work(
                session, pool_id=unit.id
            ):
                raise ConflictError("compute pool still has active workloads")
            if desired_machines > 0 and desired_machines >= unit.desired_machines:
                purchase_offer = self._available_unit_offer(provider, unit)
                if rejection := self.pooled_offer_rejection(
                    provider,
                    purchase_offer,
                    preemptible=unit.worker_preemptible,
                    now=current_time,
                ):
                    raise ConflictError(rejection)
            if unit.provider_state.degraded_reason is not None:
                # An explicit capacity mutation supersedes the durable degraded
                # reason and re-enables capacity restoration.
                unit = unit.model_copy(
                    update={
                        "provider_state": unit.provider_state.model_copy(
                            update={"degraded_reason": None, "degraded_at": None}
                        )
                    }
                )
            available = self._available_fleet_machines(
                units,
                platform_fleet=unit.platform_fleet,
                gpu=_pool_gpu_capacity(unit),
                current=unit,
            )
            if desired_machines < unit.min_machines:
                raise InvalidInputError(
                    f"compute pool {unit!r} requires at least {unit.min_machines} machines"
                )
            if (
                available is not None
                and desired_machines > available
                and desired_machines > unit.desired_machines
            ):
                raise ConflictError(f"fleet capacity limit leaves {available} machines available")
            if self._running_cpu_exceeded(units, unit, desired_machines):
                raise ConflictError("fleet running vCPU limit reached")
            maximum = (
                max(unit.max_machines, desired_machines, 1)
                if available is None
                else max(available, desired_machines, 1)
            )
            if desired_machines == 0:
                unit = unit.model_copy(
                    update={
                        "replacement_machine_id": "",
                        "replacement_template_version": "",
                    }
                )
                operations = ComputeCapacityOperationRepository(session)
                for operation in operations.list_open_for_owner(unit.capacity_owner_id):
                    operations.upsert(
                        operation.model_copy(
                            update={
                                "status": CapacityOperationStatus.Released,
                                "owns_capacity": False,
                                "release_desired_unit": 0,
                                "last_error": "",
                                "failure_count": 0,
                                "updated_at": current_time,
                            }
                        )
                    )
            if desired_machines == 0 and _zero_capacity_converged(unit):
                verify_provider_zero = True
                intent = unit
            else:
                intent = units.update_capacity(
                    unit.id,
                    expected_generation=unit.generation,
                    desired_machines=desired_machines,
                    max_machines=maximum,
                    observed_machines=unit.observed_machines,
                    phase=ComputeUnitPhase.Updating,
                    provider_state=unit.provider_state,
                    replacement_machine_id=unit.replacement_machine_id,
                    replacement_template_version=unit.replacement_template_version,
                    stopped_machines=(
                        unit.stopped_machines
                        - _reserves_resumed(reserves, unit, desired=desired_machines)
                        if reserves is not None
                        else None
                    ),
                )
                if intent is None:
                    raise ConflictError(f"compute pool {unit!r} capacity intent was superseded")
                if purchase_offer is not None:
                    intent = units.upsert(record_purchase_terms(intent, purchase_offer))

        try:
            provider, offer = self._resolved_internal_unit_provider(intent)
            if provider.pooled is None:
                raise UpstreamUnavailableError(f"compute pool {unit!r} provider is not pooled")
            if verify_provider_zero:
                observed = provider.pooled.describe_unit(self._provider_unit_request(intent, offer))
                if _provider_zero_capacity_converged(observed):
                    return self.provider_machines._apply_pooled_snapshot(
                        intent,
                        offer,
                        observed,
                        provider=provider.pooled,
                        now=current_time,
                    )
                intent = self.provider_machines._persist_zero_capacity_repair(
                    intent,
                    maximum=maximum,
                    observed=observed,
                )
            provider_request = self._provider_unit_request(intent, offer)
            if purchase_offer is not None:
                offer = purchase_offer
                provider_request = self._provider_unit_request(intent, offer)
            snapshot = provider.pooled.set_unit_capacity(
                provider_request,
                desired_machines=provider_request.desired_machines,
                max_machines=provider_request.max_machines,
            )
            return self.provider_machines._apply_pooled_snapshot(
                intent,
                offer,
                snapshot,
                provider=provider.pooled,
                now=current_time,
            )
        except ConflictError:
            raise
        except Exception as exc:
            self._mark_pooled_capacity_degraded(intent)
            if isinstance(exc, InvalidInputError | UpstreamUnavailableError):
                raise
            raise UpstreamUnavailableError(
                f"compute pool {unit!r} provider capacity update failed"
            ) from exc

    def describe_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> tuple[ComputeUnitRecord, ProviderUnitSnapshot]:
        unit, provider, offer = self._internal_unit_provider(workspace_id, capacity_owner_id)
        if provider.pooled is None:
            raise RuntimeError("internal compute unit does not use pooled capacity")
        snapshot = provider.pooled.describe_unit(self._provider_unit_request(unit, offer))
        updated = self.provider_machines._apply_pooled_snapshot(
            unit,
            offer,
            snapshot,
            provider=provider.pooled,
        )
        return updated, snapshot

    def inspect_internal_unit(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> tuple[ComputeUnitRecord, ProviderUnitSnapshot]:
        """Read provider state without entering the capacity mutation boundary."""

        unit, provider, offer = self._internal_unit_provider(workspace_id, capacity_owner_id)
        if provider.pooled is None:
            raise RuntimeError("internal compute unit does not use pooled capacity")
        snapshot = provider.pooled.describe_unit(self._provider_unit_request(unit, offer))
        return unit, snapshot

    def provider_machine_unit(self, machine_id: str) -> tuple[str, str] | None:
        """The unit and workspace owning a provider machine, if a unit owns it."""
        with self.context.database.session() as session:
            instance = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
            unit = (
                ComputeUnitRepository(session).get(instance.pool_id)
                if instance is not None and instance.pool_id is not None
                else None
            )
        return (unit.id, unit.workspace_id) if unit is not None else None

    def worker_availability_zone(self, *, unit: ComputeUnitRecord, machine_id: str) -> str:
        if unit.capacity_owner_kind is not CapacityOwnerKind.PooledProvider:
            return ""
        with self.context.database.session() as session:
            instance = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
        if instance is None or instance.pool_id != unit.id:
            raise ConflictError("worker has no provider instance in its capacity unit")
        return instance.availability_zone

    def worker_egress_policy(
        self, *, workspace_id: str, capacity_owner_id: str, machine_id: str
    ) -> WorkerEgressPolicy:
        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(capacity_owner_id)
        if unit is None or unit.workspace_id != workspace_id:
            raise ConflictError("worker network policy has no workspace-owned capacity unit")
        if not unit.platform_fleet:
            return WorkerEgressPolicy(
                billing_owner=(
                    UsageBillingOwner.ConnectedCloud
                    if unit.provider_connection_id
                    else UsageBillingOwner.SelfHosted
                ),
                verified_at=utc_now(),
            )
        with self.context.database.session() as session:
            bindings = ComputeProviderInstanceRepository(session).machine_bindings_for_pool(unit.id)
        instances = [instance for instance, machine in bindings.items() if machine == machine_id]
        if len(instances) != 1:
            raise ConflictError("worker has no unique provider instance in its capacity unit")
        provider, _ = self._resolved_internal_unit_provider(unit)
        if provider.pooled is None:
            raise UpstreamUnavailableError("worker provider cannot verify its network routes")
        try:
            destinations = provider.pooled.unbilled_network_destinations(unit, instances[0])
        except Exception as exc:
            raise UpstreamUnavailableError("worker provider route evidence is unavailable") from exc
        return WorkerEgressPolicy(
            billing_owner=UsageBillingOwner.PlatformFleet,
            routes=destinations,
            verified_at=utc_now(),
        )

    def internal_unit_machine_by_instance(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> dict[str, str]:
        """Which machine each of a unit's provider instances became.

        A provider snapshot names instances; everything downstream of enrollment
        names machines, and only the provider instance record holds both. An
        instance that has not enrolled yet has no machine and is absent here
        rather than present with an empty value.
        """
        unit = self.get_internal_unit(workspace_id, capacity_owner_id)
        with self.context.database.session() as session:
            return ComputeProviderInstanceRepository(session).machine_bindings_for_pool(unit.id)

    def begin_internal_unit_replacement(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
        *,
        template_version: str,
    ) -> ComputeUnitRecord:
        """Durably pair one retiring machine with one operational surge."""

        if not machine_id:
            raise InvalidInputError("replacement machine is required")
        if not template_version:
            raise InvalidInputError("planned replacement requires a template version")
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            initial = _require_internal_pooled_unit(
                units.get_by_capacity_owner_id(capacity_owner_id), unit_ref=capacity_owner_id
            )
            if initial.platform_fleet:
                units.lock_platform_capacity()
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
            self._require_maintenance_available(session, unit, machine_id="")
            provider_machine = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
            if provider_machine is None or provider_machine.pool_id != unit.id:
                raise NotFoundError(f"provider machine not found in compute unit: {machine_id}")
            if not _provider_machine_can_be_replaced(provider_machine):
                raise ConflictError("retired provider machine cannot start a replacement")
            available = self._available_fleet_machines(
                units,
                platform_fleet=unit.platform_fleet,
                gpu=_pool_gpu_capacity(unit),
                current=unit,
            )
            if available is not None and unit.desired_machines + 1 > available:
                raise CapacityLimitReachedError("fleet capacity limit prevents a replacement node")
            return units.upsert(
                unit.model_copy(
                    update={
                        "replacement_machine_id": machine_id,
                        "stopped_machines": (
                            min(
                                unit.stopped_machines, max(available - unit.desired_machines - 1, 0)
                            )
                            if available is not None
                            else unit.stopped_machines
                        ),
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
        unit = self.get_internal_unit(workspace_id, capacity_owner_id)
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
            if unit.platform_fleet:
                self._require_maintenance_available(session, unit, machine_id=machine_id)
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

    def _require_maintenance_available(
        self, session: DatabaseSession, unit: ComputeUnitRecord, *, machine_id: str
    ) -> None:
        candidates = (
            ComputeUnitRepository(session).list_platform_internal(gpu=_pool_gpu_capacity(unit))
            if unit.platform_fleet
            else [unit]
        )
        recovering = CapacityRecoveryRepository(session).active_source_units()
        if any(item.id in recovering for item in candidates):
            raise ConflictError("capacity interruption recovery takes precedence over maintenance")
        if any(item.replacement_machine_id for item in candidates):
            raise ConflictError("capacity maintenance is already in progress")
        if self._worker_update_in_progress(session, candidates, excluding_machine_id=machine_id):
            raise ConflictError("a worker update is already in progress")

    def _worker_update_in_progress(
        self,
        session: DatabaseSession,
        candidates: Sequence[ComputeUnitRecord],
        *,
        excluding_machine_id: str = "",
    ) -> bool:
        if self.scheduler_hooks is None:
            raise UpstreamUnavailableError("capacity maintenance requires scheduler worker state")
        for candidate in candidates:
            # A machine that is stopping, stopped or terminating cannot finish an
            # update, so an update it still records would hold maintenance for the
            # whole fleet until someone removed the machine by hand.
            for record in ComputeProviderInstanceRepository(session).list_for_pool(
                candidate.id,
                excluded_statuses=(
                    ReservationStatus.Deleted.value,
                    ReservationStatus.Failed.value,
                    ReservationStatus.Terminating.value,
                    ReservationStatus.Stopping.value,
                    ReservationStatus.Stopped.value,
                ),
            ):
                if (
                    record.machine_id is not None
                    and record.machine_id != excluding_machine_id
                    and (
                        WorkerReleaseRepository(session).machine_has_update(record.machine_id)
                        or self.scheduler_hooks.machine_has_worker_update(
                            candidate.capacity_owner_id, record.machine_id
                        )
                    )
                ):
                    return True
        return False

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
        """When each of a unit's machines stopped accepting work.

        Read rather than inferred from the scheduler's worker records: a worker is
        `Unavailable` for a disconnect or a failed registration just as readily as
        for maintenance, and only the enrollment says which.
        """
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

    def recovery_protected_machines(self, capacity_owner_id: str) -> set[str]:
        with self.context.database.session() as session:
            return CapacityRecoveryRepository(session).protected_sources(capacity_owner_id)

    def reconcile_capacity_recovery(self, *, now: datetime, limit: int = 16) -> None:
        from compute.capacity_recovery import CapacityRecoveryService

        CapacityRecoveryService(self).reconcile(now=now, limit=limit)

    def drain_internal_unit_machine(
        self,
        workspace_id: str,
        machine_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        """Stop a machine being given new work, durably.

        Narrow on purpose: capacity state, its reason, and when it was observed,
        and nothing else. The enrollment is where a planned drain survives. The
        scheduler's worker record is rewritten by reconcile passes and by the node
        itself, so a drain written only there is undone by whichever runs next.

        Setting it is the whole of stopping the machine. Schedulability reads it,
        so the worker is disabled on the next pass and stays disabled; the
        capacity interruption source reads it too, and requeues the work that can
        move. Returns whether anything changed, so a caller that runs every pass
        does not rewrite an observed-at that other timing is measured from.
        """
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
                    workspace_changes=self.workspace_changes,
                    workspace_id=workspace_id,
                    message=reason,
                    now=current_time,
                )
        return True

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
        booted_since_prepared: bool,
        prepared_stop: MachineStopPreparationReceipt | None,
    ) -> ReserveAgentPreparation:
        """Authenticate preparation evidence and keep retained hosts out of intake."""
        release_agent, release_image = reserve_release_artifacts(release)
        agent_current = not release_agent or agent_binary_sha256 == release_agent
        worker_prepared = release_image in prepared_worker_images
        with self.context.database.session() as session:
            record = ComputeProviderInstanceRepository(session).get_by_machine(machine_id)
            if record is None or record.pool_id is None or record.instance_id is None:
                return ReserveAgentPreparation()
            preparing = record.status in PREPARED_RESERVE_STATUSES
            resuming = record.status == ReservationStatus.Resuming.value
            if not preparing and not resuming:
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
            if resuming and record.resume_authorized_at is not None:
                # An earlier stream authorized the resume; the machine serves, and
                # only that stream tells the agent it resumed.
                return ReserveAgentPreparation()
            # Only a used machine returning to reserve cleans tenant storage before
            # it stops, and returning sets its row to stopping. A reserve being
            # prepared again keeps its row at preparing until it finishes, even
            # after it once served, so its stop goes through preparation.
            stop_request_id = (
                machine.lifecycle_at.isoformat()
                if machine.lifecycle is MachineLifecycle.Stopping
                and record.first_served_at is not None
                and record.status == ReservationStatus.Stopping.value
                else ""
            )
            stopping_used_machine = bool(stop_request_id)
            # A machine that booted after its preparation was stopped and started
            # again, so a row still reading stopping or stopped lags the resume.
            # Its worker waits, fenced, like a hibernating reserve's, until the
            # row reads resuming.
            lagging_resume = (
                booted_since_prepared
                and not stopping_used_machine
                and record.status
                in {
                    ReservationStatus.Stopping.value,
                    ReservationStatus.Stopped.value,
                }
            )
            hibernate = preparing and record.hibernates and not stopping_used_machine
            warm = hibernate or lagging_resume
            instruction = ReserveAgentPreparation(
                preparing=preparing,
                warm=warm,
                stop_request_id=stop_request_id,
                resuming=resuming,
                lagging_resume=lagging_resume,
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
                # Only the stream that authorizes the resume says so.
                return replace(instruction, resuming=False)
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
                    workspace_changes=self.workspace_changes,
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
                ComputeProviderInstanceRepository(session).authorize_resume(record.id)
        if resuming:
            return instruction
        try:
            self._finish_reserved_machine_preparation(
                workspace_id=workspace_id,
                machine_id=machine_id,
                capacity_owner_id=unit.capacity_owner_id,
                instance_id=record.instance_id,
                prepared_stop=prepared_stop if stopping_used_machine else None,
                hibernate=hibernate,
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

    def _finish_reserved_machine_preparation(
        self,
        *,
        workspace_id: str,
        machine_id: str,
        capacity_owner_id: str,
        instance_id: str,
        prepared_stop: MachineStopPreparationReceipt | None,
        prepared_release: tuple[str, str] | None,
        hibernate: bool,
    ) -> None:
        with self._required_capacity_owner_mutations().mutation_lock(capacity_owner_id):
            current, provider, offer = self._internal_unit_provider(workspace_id, capacity_owner_id)
            if provider.pooled is None or current.phase in ENDED_UNIT_PHASES:
                raise ConflictError("reserve preparation owner is no longer active")
            if prepared_stop is not None:
                if self.scheduler_hooks is None:
                    raise UpstreamUnavailableError(
                        "stopping retained capacity requires scheduler worker state"
                    )
                with self._required_capacity_owner_mutations().dispatch_lock(capacity_owner_id):
                    with self.context.database.session() as session:
                        latest = MachineRepository(session).get(
                            machine_id, workspace_id=workspace_id
                        )
                        if (
                            latest is None
                            or latest.lifecycle is not MachineLifecycle.Stopping
                            or latest.lifecycle_at.isoformat() != prepared_stop.request_id
                            or ContainerRepository(session).count_live_for_machine(machine_id)
                        ):
                            raise ConflictError("stop preparation was superseded")
                    self.source_cache_lifecycle.acknowledge_machine_cleanup(
                        machine_id=machine_id,
                        worker_id=agent_machine_worker_id(machine_id),
                        generation_id=prepared_stop.cache_generation_id,
                        session_fence=prepared_stop.cache_session_fence,
                        observed_at=utc_now(),
                    )
                    self.scheduler_hooks.disable_machine(
                        machine_id, "machine returning to stopped reserve"
                    )
                    snapshot = provider.pooled.stop_machine(
                        self._provider_unit_request(current, offer), instance_id
                    )
                    self.provider_machines._apply_pooled_snapshot(
                        current, offer, snapshot, provider=provider.pooled
                    )
            else:
                # Applied in the stream that finished the preparation, so the row
                # reads stopping or active without waiting for a capacity pass.
                snapshot = provider.pooled.complete_machine_preparation(
                    self._provider_unit_request(current, offer), instance_id, hibernate=hibernate
                )
                self.provider_machines._apply_pooled_snapshot(
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

    def release_internal_unit_machine(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
    ) -> ComputeUnitRecord:
        unit, provider, offer = self._internal_unit_provider(workspace_id, capacity_owner_id)
        if provider.pooled is None:
            raise RuntimeError("internal compute pool does not use pooled capacity")
        market = unit_reserve_market(
            preemptible=unit.worker_preemptible, gpu_type=unit.worker_gpu_type
        )
        stopped_target = (
            targets.stopped_target
            if self.reserve_state is not None
            and (targets := self.reserve_state.published().targets.get(market.key)) is not None
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
                gpu = _pool_gpu_capacity(current)
                can_retain = (
                    desired
                    + retained
                    + current.retiring_stopped_machines
                    + units.platform_capacity_usage(gpu=gpu, excluding_unit_id=current.id)
                    <= self.fleet_policy.machine_limit(gpu=gpu)
                    and not held.covers(stopped_target)
                )
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
                if ContainerRepository(session).count_live_for_machine(machine_id):
                    raise ConflictError("machine must finish its workloads before stopping")
                if running <= desired:
                    raise ConflictError("machine is still required by the running capacity target")
                current = units.upsert(
                    current.model_copy(
                        update={
                            "desired_machines": desired,
                            "stopped_machines": retained,
                            "max_machines": max(current.max_machines, desired + retained),
                            "generation": current.generation + 1,
                        }
                    )
                )
                instances.upsert(
                    record.model_copy(update={"status": ReservationStatus.Stopping.value})
                )
                write_machine_lifecycle(
                    session,
                    machine,
                    MachineLifecycle.Stopping,
                    workspace_changes=self.workspace_changes,
                    workspace_id=workspace_id,
                    message="Waiting for tenant storage cleanup before stopping",
                )
                return current
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
                self.provider_machines._terminate_provider_record(
                    session,
                    record,
                    clients={},
                    reason="idle_pool_scale_down",
                    message="machine selected for named retirement after draining",
                )
        snapshot = provider.pooled.release_machine(
            self._provider_unit_request(current, offer),
            record.instance_id,
        )
        return self.provider_machines._apply_pooled_snapshot(
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

    def empty_joined_units(self) -> list[ComputeUnitRecord]:
        with self.context.database.session() as session:
            return ComputeUnitRepository(session).empty_joined_units()

    def delete_empty_joined_unit(self, unit: ComputeUnitRecord) -> bool:
        leases = self._required_capacity_owner_mutations()
        with (
            leases.mutation_lock(unit.capacity_owner_id),
            leases.dispatch_lock(unit.capacity_owner_id),
            self.context.database.session() as session,
        ):
            workspace = WorkspaceRepository(session).get(unit.workspace_id)
            if workspace is None:
                return False
            if workspace.status is not WorkspaceStatus.Active:
                return False
            WorkspaceRepository(session).lock_active_owner(unit.workspace_id)
            units = ComputeUnitRepository(session)
            current = units.get_by_capacity_owner_id(unit.capacity_owner_id, for_update=True)
            if (
                current is None
                or current.workspace_id != unit.workspace_id
                or current.provider != "agent"
                or current.platform_fleet
                or current.capacity_owner_kind is not CapacityOwnerKind.WorkspaceAgent
                or leases.has_open_reservations(current.capacity_owner_id)
            ):
                return False
            if ComputeMachineEnrollmentRepository(session).list_for_unit(
                current.workspace_id, current.capacity_owner_id
            ):
                return False
            credentials = ComputeJoinCredentialRepository(session)
            issued = credentials.list_for_unit(
                current.workspace_id, current.capacity_owner_id, for_update=True
            )
            now = utc_now()
            if not issued or any(
                credential.status is ComputeCredentialStatus.Active
                and credential.expires_at > now
                and credential.use_count < credential.max_uses
                for credential in issued
            ):
                return False
            containers = ContainerRepository(session)
            machines = MachineRepository(session).list_for_capacity_owner(
                current.workspace_id, current.capacity_owner_id
            )
            if any(
                machine.lifecycle is not MachineLifecycle.Deleted
                or containers.count_live_for_machine(machine.id)
                for machine in machines
            ):
                return False
            credentials.delete_for_unit(current.workspace_id, current.capacity_owner_id)
            units.delete(current.id, workspace_id=current.workspace_id)
        self._publish_change(
            workspace_id=unit.workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=WorkspaceChangeType.Deleted,
            resource_id=unit.capacity_owner_id,
        )
        return True

    def reconcile_pooled_capacity(
        self,
        *,
        now: datetime | None = None,
    ) -> list[ComputeUnitRecord]:
        current_time = _utc(now)
        self._required_capacity_owner_mutations()
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

    def reconcile_platform_reserves(
        self, *, now: datetime, early: bool = False
    ) -> FleetReservePlan | None:
        """Plan every market's headroom and apply it, on one replica at a time.

        Each replica calls this every minute and one of them plans; sustained
        pressure brings the next plan forward. A quiet fleet costs one snapshot
        statement.
        """
        if self.provider_resolver is None or self.reserve_state is None:
            return None
        if not self.reserve_state.claim_plan(early=early):
            return None
        try:
            with self._required_capacity_owner_mutations().mutation_lock(_RESERVE_OWNER):
                started = time.monotonic()
                try:
                    return self._reconcile_platform_reserves(now=now)
                finally:
                    LOGGER.info("platform reserve planning took %.3fs", time.monotonic() - started)
        except CapacityReservationLockContendedError:
            LOGGER.debug("platform reserve planning deferred: its lease is held")
            return None

    def observe_reserve_pressure(
        self, free: Mapping[ReserveMarket, Capacity], *, now: datetime
    ) -> bool:
        """Whether a market's running headroom has stayed short long enough to plan early.

        `free` is what each market's workers can still take. Markets the last plan
        gave no running target are never short.
        """
        if self.reserve_state is None:
            return False
        targets = self.reserve_state.published().targets
        ready = False
        for key, target in targets.items():
            market = ReserveMarket.parse(key)
            short = not free.get(market, Capacity()).covers(target.warm_target)
            ready = (
                self.reserve_state.pressure_ready(
                    market,
                    under_pressure=short,
                    now=now,
                    sustained_seconds=self.fleet_policy.pressure_seconds,
                )
                or ready
            )
        return ready

    def _reconcile_platform_reserves(self, *, now: datetime) -> FleetReservePlan:
        assert self.provider_resolver is not None and self.reserve_state is not None
        providers = tuple(self.provider_resolver.list_platform_providers())
        purchasable = frozenset(
            provider.ref
            for provider in providers
            if provider.pooled is not None
            and provider.policy is not None
            and provider.policy.can_purchase
        )
        with self.context.database.session() as session:
            rows = ComputeUnitRepository(session).platform_reserve_rows()
        snapshot = fleet_reserve_snapshot(
            rows, self.fleet_policy, purchasable_providers=purchasable, now=now
        )
        plan = self._plan_reserves(snapshot, now=now)
        self.reserve_state.publish(plan)
        units = {unit.id: unit for unit in rows.units}
        markets = {unit.unit_id: unit.market for unit in snapshot.units}
        for market in plan.markets:
            self._log_market_plan(market)
            zones = frozenset(
                instance.availability_zone
                for instance in rows.instances
                if markets[instance.unit_id] == market.market
                and instance.status != ReservationStatus.Terminating.value
            )
            try:
                self._apply_market_plan(market, units, providers, zones=zones, now=now)
            except CapacityReservationLockContendedError:
                LOGGER.debug("platform reserve for %s deferred: a lease is held", market.market.key)
            except CapacityReservationLeaseLostError:
                raise
            except (CapacityLimitReachedError, ConflictError, UpstreamUnavailableError) as exc:
                LOGGER.warning("platform reserve for %s not applied: %s", market.market.key, exc)
            except Exception:
                LOGGER.exception("platform reserve for %s failed", market.market.key)
        for unit in rows.units:
            if unit.phase is not ComputeUnitPhase.Degraded or not unit.desired:
                continue
            serving = sum(
                machine.unit_id == unit.id and machine.state is ReserveMachineState.Serving
                for machine in snapshot.machines
            )
            try:
                self._release_unclaimed_failed_capacity(unit.id, serving=serving, now=now)
            except CapacityReservationLockContendedError:
                continue
            except Exception:
                LOGGER.exception("releasing failed capacity in %s failed", unit.id)
        return plan

    def _plan_reserves(self, snapshot: FleetReserveSnapshot, *, now: datetime) -> FleetReservePlan:
        """Plan, then plan again without growth where work is already waiting.

        Waiting work is read only when a plan grows, so a settled fleet costs no
        second statement.
        """
        assert self.reserve_state is not None
        markets = {unit.unit_id: unit.market for unit in snapshot.units}
        conditions = ReserveConditions(
            now=now,
            lightly_used_since=self.reserve_state.published().lightly_used_since,
            recovering=frozenset(
                markets[machine.unit_id]
                for machine in snapshot.machines
                if machine.protected and machine.state is ReserveMachineState.Draining
            ),
            consolidating=self.reserve_state.cooling_markets(),
        )
        plan = plan_market_reserve(self.fleet_policy, snapshot, conditions)
        if not any(market.growth for market in plan.markets):
            return plan
        with self.context.database.session() as session:
            demand = ContainerRepository(session).unplaced_platform_demand()
        cards = frozenset(normalize_gpu_type(card) for card in demand.gpu_types)
        # Every market with waiting work, not only the ones that grew: holding one
        # back returns budget that another market's growth may then spend.
        waiting = frozenset(
            market.market
            for market in plan.markets
            if (
                bool(cards & {market.market.gpu_type, GPU_ANY})
                if market.market.gpu_type
                else demand.preemptible_cpu
                if market.market.preemptible
                else demand.cpu
            )
        )
        if not waiting:
            return plan
        return plan_market_reserve(self.fleet_policy, snapshot, replace(conditions, demand=waiting))

    def _log_market_plan(self, plan: MarketReservePlan) -> None:
        # Every replica plans in turn; a line per change rather than per pass.
        growth = (
            f"{plan.growth.kind.value} {plan.growth.role.value}"
            f"{f' in {plan.growth.unit_id}' if plan.growth.unit_id else ''}"
            if plan.growth is not None
            else "none"
        )
        decision = (
            f"{'quiet' if plan.quiet else 'loaded'}, load {_describe_capacity(plan.load)}, "
            "running free "
            f"{_describe_capacity(plan.warm_free)} of {_describe_capacity(plan.warm_target)}, "
            f"stopped {_describe_capacity(plan.stopped_capacity)} of "
            f"{_describe_capacity(plan.stopped_target)}, growth {growth}, "
            f"consolidating {plan.consolidate or plan.consolidation_candidate or 'none'}"
        )
        if decision != self._reserve_decisions.get(plan.market):
            self._reserve_decisions[plan.market] = decision
            LOGGER.info("platform reserve for %s: %s", plan.market.key, decision)

    def _apply_market_plan(
        self,
        plan: MarketReservePlan,
        units: Mapping[str, PlatformReserveUnitRow],
        providers: Sequence[ResolvedComputeProvider],
        *,
        zones: frozenset[str],
        now: datetime,
    ) -> None:
        for unit_id, retained in plan.retained.items():
            if retained != units[unit_id].retained:
                self._retain_machines(unit_id, retained)
        for unit_id, stopped in plan.stopped.items():
            if stopped < units[unit_id].stopped:
                self._set_stopped_reserves(unit_id, stopped, now=now)
        growth = plan.growth
        if growth is None:
            return
        if growth.kind is GrowthKind.Resume:
            current = self.get_internal_unit(units[growth.unit_id].workspace_id, growth.unit_id)
            LOGGER.info("resuming a %s reserve for %s headroom", growth.role.value, plan.market.key)
            self.scale_internal_unit(
                current.workspace_id,
                current.id,
                current.desired_machines + 1,
                before_mutation=_policy_owned_scale,
                now=now,
            )
            return
        reserve = growth.kind is GrowthKind.Prepare
        held = (
            frozenset(unit.id for unit in units.values() if unit.stopped)
            if reserve
            else frozenset[str]()
        )
        chosen = self._reserve_offer(
            plan.market,
            growth.role,
            providers,
            reserve=reserve,
            preferred=held,
            occupied_zones=frozenset() if reserve else zones,
            now=now,
        )
        if chosen is None:
            LOGGER.warning(
                "no approved %s offer can supply %s headroom", growth.role.value, plan.market.key
            )
            return
        provider, offer = chosen
        policy = provider.policy
        assert policy is not None
        owner_id = self.pooled_offer_owner_id(provider, offer)
        with self._required_capacity_owner_mutations().mutation_lock(owner_id):
            unit = self._prepare_pooled_offer(
                provider=provider,
                offer=offer,
                requirements=ComputeResourceRequirements(
                    preemptible=plan.market.preemptible,
                    gpu=[offer.gpu] if offer.gpu else [],
                    gpu_count=offer.gpu_count,
                ),
                desired_machines=0,
                root_volume_gib=policy.root_volume_gib,
                idle_timeout_seconds=policy.idle_timeout_seconds,
                baseline=None,
                now=now,
            )
            LOGGER.info(
                "%s %s for %s headroom",
                "preparing a stopped reserve on" if reserve else "buying",
                offer.id,
                plan.market.key,
            )
            if reserve:
                self._add_stopped_reserve(unit.id, now=now)
            else:
                self.scale_internal_unit(
                    unit.workspace_id,
                    unit.capacity_owner_id,
                    unit.desired_machines + 1,
                    before_mutation=_policy_owned_scale,
                    now=now,
                )

    def _reserve_offer(
        self,
        market: ReserveMarket,
        role: MachineRole,
        providers: Sequence[ResolvedComputeProvider],
        *,
        reserve: bool,
        preferred: frozenset[str],
        occupied_zones: frozenset[str],
        now: datetime,
    ) -> tuple[ResolvedComputeProvider, ComputeOffer] | None:
        """The offer a reserve buys or prepares in: the role's cheapest, in a healthy market.

        Units already holding reserves come first, then regions in the order the
        provider prefers them with regions refusing launches last, then zones the
        market does not run in yet, so one zone's interruptions reach less of it.
        """
        candidates: list[tuple[ResolvedComputeProvider, ComputeOffer]] = []
        for provider in providers:
            policy = provider.policy
            if provider.pooled is None or policy is None or not policy.can_purchase:
                continue
            try:
                listing = (
                    provider.pooled.list_reserve_offers(root_volume_gib=policy.root_volume_gib)
                    if reserve
                    else provider.pooled.list_offers(root_volume_gib=policy.root_volume_gib)
                )
                candidates.extend(
                    (provider, offer)
                    for offer in listing
                    if offer.provider == provider.ref
                    and offer.preemptible is market.preemptible
                    and bool(offer.gpu_count) == bool(market.gpu_type)
                    and (
                        not market.gpu_type
                        or normalize_gpu_type(offer.gpu or "") == market.gpu_type
                    )
                    and self.fleet_policy.role(
                        cpu_millicores=offer.cpu_millicores,
                        memory_mib=offer.memory_mb,
                        gpu_count=offer.gpu_count,
                    )
                    is role
                    and offer.storage_mb >= policy.root_volume_gib * 1024
                    and offer.cost_terms.complete_hourly_cost_micros is not None
                    and self.pooled_offer_rejection(
                        provider, offer, preemptible=market.preemptible, now=now
                    )
                    is None
                )
            except Exception:
                LOGGER.exception("platform offer discovery failed for %s", provider.ref)
        if not candidates:
            return None
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
        with self.context.database.session() as session:
            states = ComputeUnitRepository(session).offer_states(tuple(identities))
        unavailable = {
            identity
            for identity, state in states.items()
            if state.phase is ComputeUnitPhase.Deleting
            or self._capacity_rejected_recently(state, now=now)
            or (
                state.provider_state.degraded_reason is not None
                and not self._failed_market_retry_ready(state, now=now)
            )
        }
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
        request = OfferRequest(nodes=1, preemptible=market.preemptible)

        def order(identity: tuple[str, str, str, str, int]) -> tuple[object, ...]:
            provider, offer = identities[identity]
            policy = provider.policy
            assert policy is not None
            state = states.get(identity)
            return (
                state is None or state.id not in preferred,
                (offer.market.cloud, offer.region) in short_regions,
                policy.region_rank(offer.region),
                offer.availability_zone in occupied_zones,
                offer.gpu_count,
                offer_selection_key(offer, request),
            )

        available = sorted((item for item in identities if item not in unavailable), key=order)
        return identities[available[0]] if available else None

    def _retain_machines(self, unit_id: str, retained: int) -> None:
        """Keep this many of the unit's serving machines from the idle drain.

        Under the unit's lease, which the drain holds while it releases a machine,
        so the count never changes between its choice and its release.
        """
        with (
            self._required_capacity_owner_mutations().mutation_lock(unit_id),
            self.context.database.session() as session,
        ):
            units = ComputeUnitRepository(session)
            units.lock_platform_capacity()
            current = units.get(unit_id, for_update=True)
            if current is None or current.phase in ENDED_UNIT_PHASES:
                return
            updated = _with_retained_machines(current, min(retained, current.desired_machines))
            if updated == current:
                return
            updated = units.upsert(updated)
        # The drain reads the count from the unit's hot state.
        if self.scheduler_hooks is not None:
            provider, offer = self._resolved_internal_unit_provider(updated)
            if provider.pooled is not None:
                self.scheduler_hooks.register_internal_unit(updated, offer)

    def _set_stopped_reserves(self, unit_id: str, stopped: int, *, now: datetime) -> None:
        with self._required_capacity_owner_mutations().mutation_lock(unit_id):
            with self.context.database.session() as session:
                units = ComputeUnitRepository(session)
                units.lock_platform_capacity()
                current = units.get(unit_id, for_update=True)
                if current is None or stopped >= current.stopped_machines:
                    return
                units.upsert(
                    current.model_copy(
                        update={
                            "stopped_machines": stopped,
                            "retiring_stopped_machines": current.retiring_stopped_machines
                            + current.stopped_machines
                            - stopped,
                            "generation": current.generation + 1,
                        }
                    )
                )
            LOGGER.info("retiring stopped reserves in %s down to %d", unit_id, stopped)
            self.reconcile_unit_capacity(unit_id, now=now)

    def _add_stopped_reserve(self, unit_id: str, *, now: datetime) -> None:
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            units.lock_platform_capacity()
            current = units.get(unit_id, for_update=True)
            if current is None or current.phase in ENDED_UNIT_PHASES:
                return
            if current.retiring_stopped_machines:
                return
            gpu = _pool_gpu_capacity(current)
            if units.platform_capacity_usage(gpu=gpu) >= self.fleet_policy.machine_limit(gpu=gpu):
                raise CapacityLimitReachedError("fleet capacity limit leaves no room for a reserve")
            units.upsert(
                current.model_copy(
                    update={
                        "stopped_machines": current.stopped_machines + 1,
                        "max_machines": max(
                            current.max_machines,
                            current.desired_machines + current.stopped_machines + 1,
                        ),
                        "generation": current.generation + 1,
                    }
                )
            )
        self.reconcile_unit_capacity(unit_id, now=now)

    def refresh_stale_reserve(self, release: ReleaseTarget, *, now: datetime) -> str | None:
        """Prepare one stopped reserve again when it predates the active release.

        A reserve resumed with an old agent or worker image updates itself before
        it takes work, which costs a devbox start tens of seconds. Refreshing it
        while nothing waits keeps that off the resume path. One machine at a time,
        and only when no reserve is being prepared, so the fleet never loses more
        than one reserve to the refresh. Returns the machine it started.
        """
        if self.provider_resolver is None:
            return None
        agent_sha256, worker_image = reserve_release_artifacts(release)
        with self.context.database.session() as session:
            candidates = ComputeProviderInstanceRepository(session).stale_platform_reserves(
                agent_sha256=agent_sha256, worker_image=worker_image
            )
        if not candidates:
            return None
        try:
            with self._required_capacity_owner_mutations().mutation_lock(_RESERVE_OWNER):
                with self.context.database.session() as session:
                    if (
                        ComputeProviderInstanceRepository(session).platform_reserve_in_preparation()
                        or ContainerRepository(session).unplaced_platform_demand().any
                    ):
                        return None
                for candidate in candidates[:_RESERVE_REFRESH_CANDIDATES]:
                    try:
                        refreshed = self._refresh_reserve(candidate, now=now)
                    except CapacityReservationLockContendedError:
                        continue
                    except CapacityReservationLeaseLostError:
                        raise
                    except Exception:
                        LOGGER.exception(
                            "stopped reserve %s (%s) could not be prepared again",
                            candidate.machine_id,
                            candidate.instance_id,
                        )
                        continue
                    if refreshed:
                        LOGGER.info(
                            "refreshing stopped reserve %s (%s) for the active release",
                            candidate.machine_id,
                            candidate.instance_id,
                        )
                        return candidate.machine_id
        except CapacityReservationLockContendedError:
            return None
        return None

    def _refresh_reserve(self, candidate: ComputeReserveInstance, *, now: datetime) -> bool:
        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get(candidate.pool_id)
        if (
            unit is None
            or unit.phase in ENDED_UNIT_PHASES
            or unit.provider_state.degraded_reason is not None
        ):
            return False
        with self._required_capacity_owner_mutations().mutation_lock(unit.capacity_owner_id):
            unit, provider, offer = self._internal_unit_provider(
                unit.workspace_id, unit.capacity_owner_id
            )
            if provider.pooled is None or provider.policy is None:
                return False
            if not provider.policy.can_purchase:
                return False
            snapshot = provider.pooled.refresh_machine(
                self._provider_unit_request(unit, offer), candidate.instance_id
            )
            with self.context.database.session() as session:
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
                        workspace_changes=self.workspace_changes,
                        workspace_id=unit.workspace_id,
                        message="Preparing the stopped reserve for the current release",
                        now=now,
                    )
            self.provider_machines._apply_pooled_snapshot(
                unit, offer, snapshot, provider=provider.pooled, now=now
            )
        return True

    @staticmethod
    def _capacity_rejected_recently(
        unit: ComputeUnitRecord | ComputeOfferState, *, now: datetime
    ) -> bool:
        """Whether the provider refused a launch in this market within its cooldown.

        A refused reserve launch does not degrade the pool, because its running
        machines still serve; this is what keeps the reserve from retrying there.
        """
        failed_at = unit.provider_state.last_capacity_failure_at
        return failed_at is not None and now < to_utc(failed_at) + timedelta(
            seconds=unit.registration_timeout_seconds
        )

    @staticmethod
    def _failed_market_retry_ready(
        unit: ComputeUnitRecord | ComputeOfferState, *, now: datetime
    ) -> bool:
        return (
            unit.provider_state.degraded_reason is not None
            and unit.provider_state.degraded_at is not None
            and not unit.desired_machines
            and not unit.observed_machines
            and unit.phase in {ComputeUnitPhase.Degraded, ComputeUnitPhase.Deleted}
            and now
            >= to_utc(unit.provider_state.degraded_at)
            + timedelta(seconds=unit.registration_timeout_seconds)
        )

    def _release_unclaimed_failed_capacity(
        self, unit_id: str, *, serving: int, now: datetime
    ) -> None:
        """Give back what a failed market bought but never served, keeping what serves.

        `serving` comes from the planner's snapshot of the same pass.
        """
        mutations = self._required_capacity_owner_mutations()
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
                    or self._machines_holding_active_work(session, pool_id=current.id)
                ):
                    return
                retained = min(current.desired_machines, current.min_machines, serving)
                intent = _with_retained_machines(current, retained).model_copy(
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
            provider, offer = self._resolved_internal_unit_provider(intent)
            if provider.pooled is None:
                raise UpstreamUnavailableError("failed capacity provider is not pooled")
            request = self._provider_unit_request(intent, offer)
            snapshot = provider.pooled.set_unit_capacity(
                request,
                desired_machines=request.desired_machines,
                max_machines=request.max_machines,
            )
            self.provider_machines._apply_pooled_snapshot(
                intent, offer, snapshot, provider=provider.pooled, now=now
            )

    def reconcile_unit_capacity(
        self, unit_id: str, *, now: datetime | None = None
    ) -> ComputeUnitRecord | None:
        mutations = self._required_capacity_owner_mutations()
        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get(unit_id)
        if unit is None:
            raise NotFoundError(f"compute unit not found: {unit_id}")
        _require_internal_pooled_unit(unit, unit_ref=unit_id)
        with mutations.mutation_lock(unit.capacity_owner_id):
            current_time = _utc(now)
            observed = self._reconcile_pooled_pool(
                unit.id, dispatch_fence=mutations, now=current_time
            )
            if observed is not None and observed.phase not in ENDED_UNIT_PHASES:
                self._reconcile_capacity_operations(observed, mutations=mutations, now=current_time)
                with self.context.database.session() as session:
                    return ComputeUnitRepository(session).get(unit.id)
            return observed

    def _reconcile_capacity_operations(
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
                        ) or self._machines_holding_active_work(session, pool_id=unit.id):
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
                self.release_acquired_capacity(
                    CapacityReleaseRequest(
                        capacity_owner_id=unit.capacity_owner_id,
                        reservation_id=operation.reservation_id,
                        operation_id=operation.operation_id,
                    )
                )

    def _reconcile_pooled_pool(
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
            self._retire_proven_provider_pool_machines(current, now=now)
            return None
        try:
            current = self._unit_matching_its_provider(current, now=now)
            provider, offer = self._resolved_internal_unit_provider(current)
            pooled = provider.pooled
            if pooled is None:
                raise UpstreamUnavailableError(
                    f"compute pool {current.name!r} provider is not pooled"
                )
            if current.phase is ComputeUnitPhase.Deleting:
                with dispatch_fence.dispatch_lock(current.capacity_owner_id):
                    snapshot = pooled.delete_unit(self._provider_unit_request(current, offer))
                return self.provider_machines._apply_pooled_snapshot(
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
                    pooled.ensure_unit(self._provider_unit_request(current, offer))
            current = self._reclaim_pooled_bootstrap_failures(
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
                        self._provider_unit_request(current, offer), record.instance_id
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
                        self._provider_unit_request(current, offer), instance_id, hibernate=False
                    )
                except Exception:
                    LOGGER.warning(
                        "finishing the resume of %s in %s failed",
                        instance_id,
                        current.id,
                        exc_info=True,
                    )
                    continue
                current = self.provider_machines._apply_pooled_snapshot(
                    current, offer, snapshot, provider=pooled, now=now
                )
            request = self._provider_unit_request(current, offer)
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
                    current = self.provider_machines._apply_pooled_snapshot(
                        current,
                        offer,
                        snapshot,
                        provider=pooled,
                        now=now,
                    )
                    return self._retire_empty_platform_pool(
                        current,
                        provider=provider,
                        offer=offer,
                        snapshot=snapshot,
                        mutations=dispatch_fence,
                        now=now,
                    )
            offer = self._available_unit_offer(provider, current)
            rejection = self.pooled_offer_rejection(
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
            request = self._provider_unit_request(current, offer)
            if degraded:
                request = request.model_copy(update={"purchases_enabled": False})
            snapshot = self._ensure_reconciled_pool(
                current,
                pooled=pooled,
                request=request,
                dispatch_fence=dispatch_fence,
            )
            return self.provider_machines._apply_pooled_snapshot(
                current,
                offer,
                snapshot,
                provider=pooled,
                now=now,
            )
        except ProviderAuthorizationPendingError as exc:
            LOGGER.info("pooled capacity reconciliation deferred for %s: %s", current.id, exc)
            return current
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

    def _retire_empty_platform_pool(
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
                or CapacityRecoveryRepository(session).unit_has_active_recovery(current.id)
                or ComputeCapacityOperationRepository(session).list_open_for_owner(
                    current.capacity_owner_id
                )
                or self._machines_holding_active_work(session, pool_id=current.id)
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
        return self.provider_machines._apply_pooled_snapshot(
            retiring,
            offer,
            pooled.delete_unit(self._provider_unit_request(retiring, offer)),
            provider=pooled,
            now=now,
        )

    @staticmethod
    def _ensure_reconciled_pool(
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

    def _unit_matching_its_provider(
        self,
        unit: ComputeUnitRecord,
        *,
        now: datetime,
    ) -> ComputeUnitRecord:
        """Reconcile the pool and ownership from its authoritative provider policy."""
        if self.provider_resolver is None:
            raise UpstreamUnavailableError("compute provider resolver is unavailable")
        provider = self.provider_resolver.resolve(unit.workspace_id, unit.provider_ref)
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

    def _pool_agents_reachable(
        self,
        session: DatabaseSession,
        pool: ComputeUnitRecord,
        records: list[ComputeProviderInstanceRecord],
        *,
        now: datetime,
    ) -> bool:
        """Whether anything in this pool is still getting through to us.

        A control plane that is up but refusing every agent looks, machine by
        machine, exactly like a pool of machines that each died at once. If a
        populated pool has nobody reporting, the fault is far more likely ours
        than theirs, and the pass says so by judging nothing.

        Two or more, because a pool of one that genuinely died would otherwise
        be immortal: with a single machine there is no majority to disagree with
        and its own silence would excuse it forever.
        """

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
            "pooled capacity reclaim observed no agent reporting in %s; judging nothing this pass",
            pool.name,
        )
        return False

    def _reclaim_pooled_bootstrap_failures(
        self,
        pool: ComputeUnitRecord,
        *,
        pooled: PooledCapacityProvider,
        offer: ComputeOffer,
        now: datetime,
    ) -> ComputeUnitRecord:
        """Reclaim pooled machines that missed their bootstrap phase deadline.

        Terminal proof stays with the pooled snapshot path: a reclaimed record
        is marked terminating here and released at the provider, and it becomes
        deleted only once ``machine_storage_destroyed`` proves the instance and
        its volumes absent during snapshot application. Exhausted relaunch
        attempts durably degrade the pool instead of relaunching forever.
        """
        hooks = self.provider_machines.scheduler_hooks
        if hooks is None:
            # Nothing can say whether these machines take work, so nothing here
            # can say they failed to. Declining is the same answer as an absent
            # heartbeat intake below, for the same reason.
            LOGGER.warning(
                "pooled capacity reclaim declined for %s: no scheduler worker state is configured",
                pool.name,
            )
            return pool
        observing_since = hooks.agent_intake_observing_since()
        if observing_since is None:
            # Nothing is receiving agent heartbeats, so every machine looks
            # silent and none of that silence is evidence. Declining is loud
            # rather than quiet: a registry that stays empty stops reclaim
            # entirely, and a machine that leaks bills until someone reads this.
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
            pool_reachable = self._pool_agents_reachable(session, pool, records, now=now)
            containers = ContainerRepository(session)
            for record in records:
                observation = self.provider_machines._observe_provider_service_state(
                    session,
                    pool,
                    record,
                    pool_reachable=pool_reachable,
                )
                observed = self.provider_machines._record_service_observation(
                    session,
                    record,
                    observation,
                    now=now,
                )
                failure = self.provider_machines._provider_bootstrap_failure_to_reclaim(
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
            >= self.reclaim.max_launch_attempts_for(record.provider)
            for record, _failure in to_reclaim
        )
        if attempts_exhausted:
            degraded = self._mark_pooled_capacity_degraded(
                current, reason="bootstrap_launch_attempts_exhausted", now=now
            )
            if degraded is not None:
                current = degraded
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
            if record.instance_id is not None:
                snapshot = pooled.release_machine(
                    self._provider_unit_request(current, offer),
                    record.instance_id,
                )
                current = self.provider_machines._apply_pooled_snapshot(
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

    def request_connection_drain(
        self,
        connection_id: str,
        *,
        workspace_ids: Sequence[str],
    ) -> AwsAccountPoolDrain:
        """Drain every unit the connection provisions, across all the owner's workspaces.

        A connection backs each of them, so a unit in any one is capacity this
        disconnect has to take down; a unit in none of them means the connection and
        the capacity disagree about who owns them.
        """
        owned = set(workspace_ids)
        mutations = self._required_capacity_owner_mutations()
        with self.context.database.session() as session:
            units = ComputeUnitRepository(session)
            # Include purchases admitted before the connection stopped accepting work.
            units.lock_platform_capacity()
            for workspace_id in sorted(owned):
                units.lock_capacity_workspace(workspace_id)
            pools = units.list_for_provider_connection(connection_id)
        if any(unit.workspace_id not in owned for unit in pools):
            raise UpstreamUnavailableError("AWS capacity ownership is inconsistent")

        for unit in pools:
            try:
                with (
                    mutations.mutation_lock(unit.capacity_owner_id),
                    mutations.dispatch_lock(unit.capacity_owner_id),
                ):
                    with self.context.database.session() as session:
                        units = ComputeUnitRepository(session)
                        if unit.platform_fleet:
                            units.lock_platform_capacity()
                        else:
                            units.lock_capacity_workspace(unit.workspace_id)
                        current = units.get(unit.id, for_update=True)
                        if current is None:
                            raise UpstreamUnavailableError("AWS capacity disappeared during drain")
                        if current.phase not in {
                            ComputeUnitPhase.Deleting,
                            ComputeUnitPhase.Deleted,
                        }:
                            current = units.upsert(
                                _with_retained_machines(current, 0).model_copy(
                                    update={
                                        "desired_machines": 0,
                                        "stopped_machines": 0,
                                        "retiring_stopped_machines": (
                                            current.retiring_stopped_machines
                                            + current.stopped_machines
                                        ),
                                        "generation": current.generation + 1,
                                        "phase": ComputeUnitPhase.Deleting,
                                        "status": ComputeUnitPhase.Deleting.value,
                                        "replacement_machine_id": "",
                                        "replacement_template_version": "",
                                    }
                                )
                            )
                    if current.phase is ComputeUnitPhase.Deleted:
                        self._retire_proven_provider_pool_machines(current, now=utc_now())
                        continue
                    provider, offer = self._resolved_internal_unit_provider(current)
                    if provider.pooled is None:
                        raise RuntimeError("AWS capacity provider is not pooled")
                    snapshot = provider.pooled.delete_unit(
                        self._provider_unit_request(current, offer)
                    )
                    self.provider_machines._apply_pooled_snapshot(
                        current,
                        offer,
                        snapshot,
                        provider=provider.pooled,
                    )
            except ConflictError:
                raise
            except Exception as exc:
                raise UpstreamUnavailableError(
                    f"AWS capacity {unit.name!r} could not be drained"
                ) from exc

        with self.context.database.session() as session:
            remaining = sum(
                unit.phase is not ComputeUnitPhase.Deleted
                for unit in ComputeUnitRepository(session).list_for_provider_connection(
                    connection_id
                )
            )
        return AwsAccountPoolDrain(total_pools=len(pools), remaining_pools=remaining)

    def list_machines(self, *, workspace: str = "default") -> list[Machine]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = [
                machine
                for machine in MachineRepository(session).list(workspace_id=workspace_id)
                if machine.lifecycle is not MachineLifecycle.Deleted
            ]
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def delete_machine(self, machine_id: str, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            machines = MachineRepository(session)
            machine = machines.get(machine_id, workspace_id=workspace_id)
            if machine is None:
                msg = f"machine not found in workspace: {machine_id}"
                raise KeyError(msg)
            if machine.lifecycle is MachineLifecycle.Deleted:
                return
            write_machine_lifecycle(
                session,
                machine,
                MachineLifecycle.Deleted,
                workspace_changes=self.workspace_changes,
                workspace_id=workspace_id,
                message="Removed",
            )

    def list_workers(self) -> list[Worker]:
        with self.context.database.session() as session:
            records = [
                worker
                for worker in WorkerRepository(session).list_across_workspaces()
                if worker.status is not ResourceStatus.Deleted
            ]
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def delete_worker(self, worker_id: str) -> None:
        with self.context.database.session() as session:
            repository = WorkerRepository(session)
            workspace_id = repository.workspace_id(worker_id)
            repository.delete_across_workspaces(worker_id)
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

    def _internal_unit_provider(
        self,
        workspace_id: str,
        capacity_owner_id: str,
    ) -> tuple[ComputeUnitRecord, ResolvedComputeProvider, ComputeOffer]:
        unit = self.get_internal_unit(workspace_id, capacity_owner_id)
        provider, offer = self._resolved_internal_unit_provider(unit)
        return unit, provider, offer

    def _resolved_internal_unit_provider(
        self,
        pool: ComputeUnitRecord,
    ) -> tuple[ResolvedComputeProvider, ComputeOffer]:
        if self.provider_resolver is None:
            raise UpstreamUnavailableError("workspace compute provider resolver is not configured")
        try:
            provider = self.provider_resolver.resolve(pool.workspace_id, pool.provider_ref)
        except ProviderAuthorizationPendingError:
            raise
        except Exception as exc:
            raise UpstreamUnavailableError(
                f"compute pool {pool.name!r} provider is unavailable"
            ) from exc
        pooled = provider.pooled
        if pooled is None:
            raise InvalidInputError(f"compute pool {pool.name!r} provider is not pooled")
        return provider, pooled.unit_offer(pool)

    @staticmethod
    def _available_unit_offer(
        provider: ResolvedComputeProvider, pool: ComputeUnitRecord
    ) -> ComputeOffer:
        pooled = provider.pooled
        if pooled is None:
            raise InvalidInputError(f"compute pool {pool.name!r} provider is not pooled")
        if provider.policy is not None and not provider.policy.can_purchase:
            return pooled.unit_offer(pool)
        offer = next(
            (
                item
                for item in pooled.list_offers(root_volume_gib=pool.root_volume_gib)
                if item.id == pool.offer_id and item.region == pool.region
            ),
            None,
        )
        if offer is None:
            raise UpstreamUnavailableError(
                f"compute pool {pool.name!r} offer is no longer available"
            )
        return offer.model_copy(update={"capability_key": pool.capability_key})

    def clear_capacity_degradation(
        self,
        workspace: str,
        capacity_owner_id: str,
    ) -> ComputeUnitRecord:
        """Let a unit that exhausted its relaunch attempts buy machines again.

        Explicit because the degraded reason exists to stop a pool billing for
        machines that never become workers; anything that cleared it as a side
        effect would defeat it. The attempt baseline moves with it, since the
        ordinal it is compared against never resets on its own and the pool
        would degrade again on its next failure regardless of the cause.
        """
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = ComputeUnitRepository(session)
            unit = repository.get_by_capacity_owner_id(capacity_owner_id, for_update=True)
            # The owner id is globally unique, so the lookup crosses workspaces;
            # a unit belonging to another one reports as missing rather than as
            # forbidden, which would confirm the id exists.
            if unit is None or unit.workspace_id != workspace_id:
                raise NotFoundError(f"compute unit {capacity_owner_id!r} not found")
            highest = ComputeProviderInstanceRepository(session).highest_launch_attempt(
                unit.id, default=unit.provider_state.launch_attempt_baseline
            )
            cleared = repository.apply_provider_state(
                unit.id,
                generation=unit.generation,
                observed_machines=unit.observed_machines,
                phase=ComputeUnitPhase.Ready,
                provider_state=unit.provider_state.model_copy(
                    update={
                        "degraded_reason": None,
                        "degraded_at": None,
                        "launch_attempt_baseline": highest,
                    }
                ),
            )
        if cleared is None:
            raise ConflictError(
                f"compute unit {capacity_owner_id!r} changed while clearing degradation"
            )
        self._publish_change(
            workspace_id=cleared.workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=WorkspaceChangeType.Updated,
            resource_id=cleared.id,
        )
        return cleared

    def _mark_pooled_capacity_degraded(
        self,
        pool: ComputeUnitRecord,
        *,
        reason: str | None = None,
        preserve_deleting: bool = False,
        now: datetime | None = None,
    ) -> ComputeUnitRecord | None:
        with self.context.database.session() as session:
            repository = ComputeUnitRepository(session)
            current = repository.get(pool.id, for_update=True)
            if current is None or current.generation != pool.generation:
                return current
            provider_state = (
                current.provider_state.model_copy(
                    update={"degraded_reason": reason, "degraded_at": to_utc(now or utc_now())}
                )
                if reason is not None
                else current.provider_state
            )
            degraded = repository.apply_provider_state(
                pool.id,
                generation=pool.generation,
                observed_machines=pool.observed_machines,
                phase=(
                    ComputeUnitPhase.Deleting
                    if preserve_deleting and pool.phase is ComputeUnitPhase.Deleting
                    else ComputeUnitPhase.Degraded
                ),
                provider_state=provider_state,
            )
        if degraded is not None:
            self._publish_change(
                workspace_id=degraded.workspace_id,
                topic=WorkspaceChangeTopic.ComputeUnits,
                change=WorkspaceChangeType.Updated,
                resource_id=degraded.id,
            )
        return degraded

    def _provider_unit_request(
        self,
        pool: ComputeUnitRecord,
        offer: ComputeOffer,
    ) -> ProviderUnitRequest:
        request = provider_unit_request(self.pool_bootstrap_factory, pool, offer)
        if self.provider_resolver is None:
            raise UpstreamUnavailableError("compute provider resolver is unavailable")
        provider = self.provider_resolver.resolve(pool.workspace_id, pool.provider_ref)
        if provider.policy is None:
            raise UpstreamUnavailableError("compute provider policy is unavailable")
        request = request.model_copy(
            update={
                "purchases_enabled": (
                    provider.policy.can_purchase and pool.provider_state.degraded_reason is None
                )
            }
        )
        if pool.provider_state.degraded_reason is None:
            return request
        with self.context.database.session() as session:
            surviving = sum(
                record.instance_id is not None and record.missing_since is None
                for record in ComputeProviderInstanceRepository(session).list_for_pool(
                    pool.id,
                    excluded_statuses=(
                        ReservationStatus.Deleted.value,
                        ReservationStatus.Failed.value,
                        ReservationStatus.Terminating.value,
                    ),
                )
            )
        return request.model_copy(
            update={"desired_machines": min(surviving, request.desired_machines)}
        )

    def _retire_proven_provider_pool_machines(
        self,
        pool: ComputeUnitRecord,
        *,
        now: datetime,
    ) -> None:
        with self.context.database.session() as session:
            records = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        for record in records:
            if (
                record.machine_id
                and record.status == ReservationStatus.Deleted.value
                and record.provider_storage_destroyed_at is not None
            ):
                self.provider_machines.retire_provider_pool_machine(
                    pool.workspace_id,
                    pool.capacity_owner_id,
                    record.machine_id,
                    reason="provider instance storage destroyed",
                    now=now,
                )


def reserve_release_artifacts(release: ReleaseTarget) -> tuple[str, str]:
    """The agent binary and worker image a reserve must hold to resume without updating."""
    return (release.agent.sha256 if release.agent is not None else "", release.worker_image)


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


def _reserves_resumed(
    snapshot: ProviderUnitSnapshot, unit: ComputeUnitRecord, *, desired: int
) -> int:
    """How many of the unit's stopped reserves raising it to `desired` will start.

    A retained pool starts a stopped reserve only when its running and stopped
    counts leave no room to launch. Lowering the stopped count by what the raise
    takes resumes that many and leaves the refill to the reserve planner, which
    decides it against the market's headroom and never ahead of waiting work;
    keeping the count launches a fresh machine beside the reserve.
    """
    if desired <= unit.desired_machines or not unit.stopped_machines:
        return 0
    stopped = sum(
        instance.status == ProviderMachineStatus.Stopped for instance in snapshot.instances
    )
    serving = sum(
        instance.status
        not in {
            ProviderMachineStatus.Preparing,
            ProviderMachineStatus.Stopping,
            ProviderMachineStatus.Stopped,
        }
        for instance in snapshot.instances
    )
    return max(
        min(
            unit.stopped_machines,
            stopped,
            desired - unit.desired_machines,
            desired - serving,
        ),
        0,
    )


def _shape_matches_pool(shape: CapacityAcquisitionShape, pool: ComputeUnitRecord) -> bool:
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
    return (
        offer.cpu_millicores == shape.cpu_millicores
        and offer.memory_mb == shape.memory_mib
        and (offer.gpu or "") == shape.gpu_type
        and offer.gpu_count == shape.gpu_count
        and offer.runtime == shape.runtime
        and offer.preemptible is shape.preemptible
    )


def _new_capacity_operation(
    pool: ComputeUnitRecord,
    request: CapacityAcquisitionRequest,
    *,
    desired_unit: int,
    status: CapacityOperationStatus,
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
        demand_container_id=request.demand_container_id,
        desired_unit=desired_unit,
        status=status,
        target_machine_id=target_machine_id,
        previous_desired_unit=previous_desired_unit,
        owns_capacity=owns_capacity,
        shape=request.shape,
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
        or (
            operation.demand_container_id is not None
            and operation.demand_container_id != request.demand_container_id
        )
        or operation.desired_unit != desired_unit
        or operation.shape != request.shape
    ):
        raise ConflictError(f"capacity operation request is immutable: {request.operation_id}")


def _validate_capacity_operation_plan(
    operation: ComputeCapacityOperationRecord,
    request: CapacityAcquisitionRequest,
) -> None:
    if (
        operation.reservation_id != request.reservation_id
        or (
            operation.demand_container_id is not None
            and operation.demand_container_id != request.demand_container_id
        )
        or operation.shape != request.shape
    ):
        raise ConflictError(f"capacity operation request is immutable: {request.operation_id}")


def _stored_capacity_status(status: CapacityOperationStatus) -> CapacityAcquisitionStatus:
    if status in {CapacityOperationStatus.Intent, CapacityOperationStatus.Releasing}:
        return CapacityAcquisitionStatus.ExistingPending
    if status.terminal:
        return CapacityAcquisitionStatus.Unsupported
    return CapacityAcquisitionStatus(status.value)


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
    max_units: int | None,
) -> CapacityAcquisitionResult:
    if max_units is not None and current_units >= max_units:
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
        owns_capacity=operation.owns_capacity,
        failure_code=failure_code or operation.failure_code,
        reason=reason or operation.last_error,
    )


def _describe_capacity(capacity: Capacity) -> str:
    described = f"{capacity.cpu_millicores / 1000:g} vCPU/{capacity.memory_mib / 1024:g} GiB"
    return f"{described}/{capacity.gpu_count} GPU" if capacity.gpu_count else described


def _with_retained_machines(unit: ComputeUnitRecord, minimum: int) -> ComputeUnitRecord:
    """The unit keeping `minimum` serving machines from the idle drain."""
    return unit.model_copy(
        update={
            "initial_machines": minimum,
            "min_machines": minimum,
            "min_free_cpu_millicores": 0,
            "min_free_memory_mib": 0,
            "min_free_gpu_count": 0,
        }
    )


def _policy_owned_scale(pool: ComputeUnitRecord) -> None:
    """Workspace policy owns the zero-capacity intent; no extra scale guard applies."""
    del pool


def _previous_policy_floor(current: ComputeUnitRecord | None) -> int:
    """The floor the stored unit was already holding.

    ``initial_machines`` matters as well as ``min_machines``: a unit whose
    initial exceeds its minimum holds that capacity durably, so reading only the
    minimum would under-release it and leave paid machines behind.
    """
    if current is None:
        return 0
    return max(current.min_machines, current.initial_machines, 0)


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


def _zero_capacity_converged(pool: ComputeUnitRecord) -> bool:
    """Durable state already says zero, so only the provider is still in doubt."""

    return (
        pool.desired_machines == 0
        and pool.observed_machines == 0
        and pool.phase is ComputeUnitPhase.Ready
    )


def _owns_provider_pool_capacity(pool: ComputeUnitRecord) -> bool:
    """Whether a provider owns the lifecycle of this capacity unit."""

    return pool.capacity_owner_kind is CapacityOwnerKind.PooledProvider and bool(pool.provider_ref)


def _pool_gpu_capacity(pool: ComputeUnitRecord) -> bool:
    return pool.worker_gpu_count > 0


def _provider_machine_can_be_replaced(record: ComputeProviderInstanceRecord) -> bool:
    # Providers may still report a retired instance while its termination runs.
    return (
        bool(record.instance_id)
        and record.status in {ReservationStatus.Pending.value, ReservationStatus.Active.value}
        and record.missing_since is None
    )


def _require_workspace_internal_pooled_unit(
    unit: ComputeUnitRecord | None,
    *,
    workspace_id: str,
    unit_ref: str,
) -> ComputeUnitRecord:
    pooled = _require_internal_pooled_unit(unit, unit_ref=unit_ref)
    if pooled.workspace_id != workspace_id:
        raise NotFoundError(f"compute unit not found: {unit_ref}")
    return pooled
