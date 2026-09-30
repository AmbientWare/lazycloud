from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from database.repositories.compute import (
    ComputeCapacityOperationRecord,
    ComputeCapacityOperationRepository,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from shared.capacity import (
    CapacityAcquisitionRequest,
    CapacityAcquisitionResult,
    CapacityAcquisitionShape,
    CapacityAcquisitionStatus,
    CapacityFailureCode,
    CapacityFulfillmentRequest,
    CapacityOperationStatus,
    CapacityOwnerKind,
    CapacityPoolSizingSnapshot,
    CapacityReleaseRequest,
    capacity_failure_message,
)
from shared.compute_policy import (
    ENDED_UNIT_PHASES,
    ComputeUnitPhase,
    ComputeUnitRecord,
)
from shared.errors import (
    ConflictError,
    UpstreamUnavailableError,
)
from shared.timestamps import to_utc, utc_now

from compute.context import ComputeContext
from compute.fleet_reserves import reserves_resumed
from compute.offers import (
    ComputeOffer,
    ReservationStatus,
    record_purchase_terms,
)
from compute.pool_provider import PoolProviderService
from compute.provider_machines import provider_unit_operational_capacity

_UNCONFIRMED_OPERATION_STATUSES = frozenset(
    {CapacityOperationStatus.Intent, CapacityOperationStatus.TemporarilyUnavailable}
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
    desired_unit: int | None = None,
) -> None:
    if (
        operation.reservation_id != request.reservation_id
        or (
            operation.demand_container_id is not None
            and operation.demand_container_id != request.demand_container_id
        )
        or (desired_unit is not None and operation.desired_unit != desired_unit)
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


@dataclass(frozen=True, slots=True)
class CapacityAcquisitionService:
    context: ComputeContext
    providers: PoolProviderService

    def ensure_capacity(
        self,
        request: CapacityAcquisitionRequest,
        *,
        minimum_unit: int = 0,
    ) -> CapacityAcquisitionResult:
        with self.context.database.session() as session:
            unit = ComputeUnitRepository(session).get_by_capacity_owner_id(
                request.capacity_owner_id
            )
            if unit is None:
                return _capacity_result(
                    request,
                    CapacityAcquisitionStatus.Unsupported,
                    desired_unit=1,
                    reason="capacity owner is not managed by compute",
                )
            operations = ComputeCapacityOperationRepository(session)
            operation = operations.get(request.capacity_owner_id, request.operation_id)
            if operation is not None:
                _validate_capacity_operation(operation, request)
                if (
                    operation.status is CapacityOperationStatus.AtLimit
                    and not operation.owns_capacity
                ):
                    operation = operations.get(
                        request.capacity_owner_id, request.operation_id, for_update=True
                    )
                    if operation is None:
                        raise ConflictError("capacity operation disappeared during retry")
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
                status = _stored_capacity_status(operation.status)
                if status not in {
                    CapacityAcquisitionStatus.Requested,
                    CapacityAcquisitionStatus.TemporarilyUnavailable,
                }:
                    return _operation_result(operation, status)
                desired = operation.desired_unit
            else:
                reason = unit.provider_state.degraded_reason
                status = CapacityAcquisitionStatus.TemporarilyUnavailable
                if reason is None and not unit.scaling_enabled:
                    reason = "capacity owner scaling is disabled"
                    status = CapacityAcquisitionStatus.Unsupported
                if reason is None and not _shape_matches_pool(request.shape, unit):
                    reason = "requested unit does not match the capacity owner's fixed worker shape"
                    status = CapacityAcquisitionStatus.Unsupported
                if reason is not None:
                    count = ComputeProviderInstanceRepository(session).count_open_for_pool(unit.id)
                    return _capacity_result(
                        request, status, desired_unit=max(count, 1), reason=reason
                    )
                pending = operations.pending_capacity_floor(request.capacity_owner_id)
                desired = (unit.desired_machines if pending is None else pending) + 1
        if unit.capacity_owner_kind is not CapacityOwnerKind.PooledProvider:
            return _capacity_result(
                request,
                CapacityAcquisitionStatus.Unsupported,
                desired_unit=1,
                reason=f"capacity owner kind {unit.capacity_owner_kind.value!r} is unsupported",
            )
        return self.acquire_pooled_capacity(unit, request, desired_unit=max(desired, minimum_unit))

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
            return self.release_pooled_capacity(unit, operation)
        return _operation_result(
            operation,
            CapacityAcquisitionStatus.Unsupported,
            reason="capacity owner does not support compute release",
        )

    def acquire_pooled_capacity(
        self,
        pool: ComputeUnitRecord,
        request: CapacityAcquisitionRequest,
        *,
        desired_unit: int,
    ) -> CapacityAcquisitionResult:
        try:
            current_pool, provider, offer = self.providers.internal_unit_provider(
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
                self.providers.provider_unit_request(current_pool, offer)
            )
        except Exception as exc:
            return self.record_capacity_failure(
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
                offer = self.providers.available_unit_offer(provider, current_pool)
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
            if rejection := self.providers.pooled_offer_rejection(
                provider, offer, preemptible=preemptible
            ):
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
                resumed = reserves_resumed(snapshot, locked_pool, desired=desired_unit)
                if (
                    resumed
                    and preemptible
                    and locked_pool.id
                    in self.providers.read_reserve_admission(session).withheld_from_preemptible
                ):
                    resumed = 0
                committed = desired_unit + locked_pool.stopped_machines - resumed
                maximum = max(locked_pool.max_machines, committed, 1)
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
            provider_request = self.providers.provider_unit_request(current_pool, offer)
            updated_snapshot = provider.pooled.set_unit_capacity(
                provider_request,
                desired_machines=provider_request.desired_machines,
                max_machines=provider_request.max_machines,
            )
            self.providers.machines._apply_pooled_snapshot(
                current_pool,
                offer,
                updated_snapshot,
                provider=provider.pooled,
            )
        except Exception as exc:
            return self.record_capacity_failure(
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

    def release_pooled_capacity(
        self,
        pool: ComputeUnitRecord,
        operation: ComputeCapacityOperationRecord,
    ) -> CapacityAcquisitionResult:
        try:
            current_pool, provider, offer = self.providers.internal_unit_provider(
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
        provider_request = self.providers.provider_unit_request(current_pool, offer)
        try:
            snapshot = provider.pooled.set_unit_capacity(
                provider_request,
                desired_machines=provider_request.desired_machines,
                max_machines=provider_request.max_machines,
            )
            self.providers.machines._apply_pooled_snapshot(
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

    def record_capacity_failure(
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

    def pool_sizing_snapshot(self, capacity_owner_id: str) -> CapacityPoolSizingSnapshot:
        """Retry the recorded pending operation. Derive release cooldowns from machines,
        since direct retirement does not create an acquisition operation."""
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
