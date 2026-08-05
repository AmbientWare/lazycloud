from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from coordination.wake_signal import WakeSignalPublisher
from pydantic import JsonValue
from shared.contracts import ContractModel
from shared.realtime.contracts import CloudEventRecord, EventDataInput, EventRecordType
from shared.scheduling import (
    SchedulerContainerCancellationResult,
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    SchedulerWorkerStatus,
    gpu_count_for_capacity,
)
from shared.timestamps import utc_now
from shared.usage import UsageMetric, UsageRecord, UsageUnit, usage_record_id

from scheduler.capacity_reservations import (
    CapacityAcquisitionResult,
    CapacityAcquisitionStatus,
    CapacityReservationConflictError,
)
from scheduler.fleet import (
    DEFAULT_MAX_SCHEDULE_RETRY_COUNT,
    DEFAULT_MAX_SCHEDULE_RETRY_DURATION,
    DEFAULT_PROVISIONING_HANDOFF,
    SchedulerRequeueAction,
    SchedulerRequeuePlan,
    plan_retry_soon,
    plan_worker_wait_requeue,
)
from scheduler.state import (
    DEFAULT_CONTAINER_REQUEST_CLAIM_LEASE_SECONDS,
    CapacityReservationDispatchAllocation,
    ConcurrencyReservationDecision,
    ConcurrencyReservationStatus,
    ContainerRequestCancelledError,
    ContainerRequestClaimNotOwnedError,
    SchedulerContainerRequestClaim,
    WorkerReservedCapacity,
    capacity_memory_mib,
)
from scheduler.tools import (
    SchedulingDecision,
    SchedulingOutcome,
    SchedulingRequest,
    WorkerCapacity,
    plan_scheduling_batch,
)

DEFAULT_SCHEDULER_REQUEUE_DELAY_SECONDS = 1.0
CONTAINER_DISPATCH_WAKE_SCOPE = "scheduler-container-dispatch"
LOGGER = logging.getLogger(__name__)


class SchedulerContainerDispatchStatus(StrEnum):
    Dispatched = "dispatched"
    Waiting = "waiting"
    Cancelled = "cancelled"
    Failed = "failed"
    Error = "error"


class SchedulerContainerDispatchResult(ContractModel):
    status: SchedulerContainerDispatchStatus
    container_id: str
    worker_id: str = ""
    reason: str = ""

    @property
    def dispatched(self) -> bool:
        return self.status is SchedulerContainerDispatchStatus.Dispatched


class SchedulerContainerStateRepository(Protocol):
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...

    def set_container_state(
        self,
        state: SchedulerContainerState,
    ) -> SchedulerContainerState: ...

    def is_container_cancelled(self, container_id: str) -> bool: ...

    def cancel_container_request(
        self,
        container_id: str,
    ) -> SchedulerContainerState | None: ...

    def delete_container_state(self, container_id: str) -> bool: ...

    def reserve_concurrency(
        self,
        *,
        workspace_id: str,
        container_id: str,
        gpu_limit: int,
        cpu_limit_millicores: int,
        request_gpu_count: int,
        request_cpu_millicores: int,
        now: datetime | None = None,
    ) -> ConcurrencyReservationDecision: ...

    def release_concurrency_reservation(
        self,
        workspace_id: str,
        container_id: str,
        *,
        now: datetime | None = None,
    ) -> ConcurrencyReservationDecision: ...


class SchedulerContainerWorkerRepository(Protocol):
    def enqueue_container_request(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> int: ...

    def claim_ready_container_requests(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1,
        lease_seconds: float = DEFAULT_CONTAINER_REQUEST_CLAIM_LEASE_SECONDS,
    ) -> list[SchedulerContainerRequestClaim]: ...

    def acknowledge_container_request(self, claim: SchedulerContainerRequestClaim) -> bool: ...

    def requeue_container_request(
        self,
        claim: SchedulerContainerRequestClaim,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime,
    ) -> bool: ...

    def has_recoverable_container_request(
        self,
        container_id: str,
        *,
        worker_id: str = "",
    ) -> bool: ...

    def list_workers(self) -> list[SchedulerWorkerRecord]: ...

    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None: ...

    def schedule_container_request(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
        *,
        reserved_capacity: WorkerReservedCapacity | None = None,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord: ...

    def dispatch_claimed_container_request(
        self,
        worker_id: str,
        claim: SchedulerContainerRequestClaim,
        *,
        reserved_capacity: WorkerReservedCapacity | None = None,
        capacity_allocation: CapacityReservationDispatchAllocation | None = None,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord: ...

    def cancel_worker_request(self, worker_id: str, container_id: str) -> bool: ...


class SchedulerContainerPlacement(Protocol):
    def place(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest: ...


class SchedulerContainerFailureHandler(Protocol):
    def mark_scheduling_failed(
        self,
        request: SchedulerWorkerRequest,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> None: ...


class SchedulerContainerAssignmentRecorder(Protocol):
    def assign_runtime(
        self,
        *,
        container_id: str,
        workspace_id: str,
        runtime_worker_id: str,
        runtime_machine_id: str,
        compute_worker_id: str | None = None,
        compute_machine_id: str | None = None,
    ) -> None: ...

    def clear_runtime_assignment(
        self,
        *,
        container_id: str,
        runtime_worker_id: str,
    ) -> None: ...


class SchedulerContainerLifecycleEvents(Protocol):
    def append_event(
        self,
        event_type: str | EventRecordType,
        data: EventDataInput,
        *,
        event_id: str | None = None,
    ) -> CloudEventRecord: ...


class SchedulerCapacityReservations(Protocol):
    def mutation_lock(
        self,
        capacity_owner_id: str,
    ) -> AbstractContextManager[None]: ...

    def resolve_request(self, request: SchedulerWorkerRequest) -> SchedulerWorkerRequest: ...

    def can_acquire(self, request: SchedulerWorkerRequest) -> bool: ...

    def acquire(
        self,
        request: SchedulerWorkerRequest,
        *,
        now: datetime | None = None,
    ) -> CapacityAcquisitionResult: ...

    def registered_worker_id(self, container_id: str) -> str: ...

    def dispatch_allocation(
        self,
        container_id: str,
    ) -> CapacityReservationDispatchAllocation | None: ...

    def prepare_dispatch(
        self,
        container_id: str,
        worker: SchedulerWorkerRecord,
        *,
        now: datetime | None = None,
    ) -> CapacityReservationDispatchAllocation | None: ...

    def reserved_worker_capacity(self) -> dict[str, WorkerReservedCapacity]: ...

    def release_request(
        self,
        container_id: str,
        *,
        workers: tuple[SchedulerWorkerRecord, ...] = (),
        now: datetime | None = None,
    ) -> None: ...


class SchedulerUsageRecorder(Protocol):
    def record(
        self,
        *,
        id: str | None = None,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        metric: UsageMetric,
        quantity: float,
        unit: UsageUnit,
        labels: dict[str, str] | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> UsageRecord: ...


@dataclass(slots=True)
class SchedulerContainerRequestService:
    workers: SchedulerContainerWorkerRepository
    containers: SchedulerContainerStateRepository
    placement: SchedulerContainerPlacement
    failure_handler: SchedulerContainerFailureHandler
    assignments: SchedulerContainerAssignmentRecorder
    dispatch_wake: WakeSignalPublisher
    lifecycle_events: SchedulerContainerLifecycleEvents
    capacity_reservations: SchedulerCapacityReservations | None = None
    usage: SchedulerUsageRecorder | None = None
    requeue_delay_seconds: float = DEFAULT_SCHEDULER_REQUEUE_DELAY_SECONDS
    max_retry_count: int = DEFAULT_MAX_SCHEDULE_RETRY_COUNT
    max_retry_age_seconds: float = DEFAULT_MAX_SCHEDULE_RETRY_DURATION.total_seconds()
    claim_lease_seconds: float = DEFAULT_CONTAINER_REQUEST_CLAIM_LEASE_SECONDS

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        current_time = ready_at or utc_now()
        quota_reserved = False
        try:
            request = self.placement.place(request)
        except Exception as exc:
            return SchedulerContainerSubmitResult(
                status=SchedulerContainerSubmitStatus.Error,
                container_id=request.container_id,
                reason=str(exc),
            )
        self._record_usage(request, UsageMetric.SchedulerContainerRequested)
        if self.containers.is_container_cancelled(request.container_id):
            return SchedulerContainerSubmitResult(
                status=SchedulerContainerSubmitStatus.Error,
                container_id=request.container_id,
                reason="container request was cancelled",
            )
        try:
            quota_error = self._reserve_quota(request, current_time)
            if quota_error:
                return SchedulerContainerSubmitResult(
                    status=SchedulerContainerSubmitStatus.Error,
                    container_id=request.container_id,
                    reason=quota_error,
                )
            quota_reserved = _request_uses_quota(request)
            self.containers.set_container_state(
                _container_state(request, scheduled_at=current_time)
            )
            self.workers.enqueue_container_request(request, ready_at=current_time)
            self._record_usage(request, UsageMetric.SchedulerContainerScheduled)
        except Exception as exc:
            release_error = ""
            if quota_reserved:
                try:
                    self.containers.release_concurrency_reservation(
                        request.workspace_id,
                        request.container_id,
                        now=current_time,
                    )
                except Exception as release_exc:  # pragma: no cover - defensive rollback path
                    release_error = (
                        f"; failed to release concurrency reservation: "
                        f"{type(release_exc).__name__}: {release_exc}"
                    )
            return SchedulerContainerSubmitResult(
                status=SchedulerContainerSubmitStatus.Error,
                container_id=request.container_id,
                reason=f"{exc}{release_error}",
            )
        self._signal_dispatch(request.container_id)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
            reason="container request queued for scheduling",
        )

    def _signal_dispatch(self, container_id: str) -> None:
        try:
            self.dispatch_wake.signal()
        except Exception:
            LOGGER.warning(
                "scheduler dispatch wake failed; periodic sweep will recover the request",
                exc_info=True,
                extra={"container_id": container_id},
            )

    def cancel(self, container_id: str) -> SchedulerContainerCancellationResult:
        state = self.containers.cancel_container_request(container_id)
        if state is None:
            self._release_capacity_reservation(container_id)
            return SchedulerContainerCancellationResult(container_id=container_id)
        terminal = state.status in {
            SchedulerContainerStatus.Complete,
            SchedulerContainerStatus.Failed,
        }
        pending_request_removed = False
        if state.worker_id and not terminal:
            pending_request_removed = self.workers.cancel_worker_request(
                state.worker_id,
                container_id,
            )
            if pending_request_removed:
                self.containers.delete_container_state(container_id)
        self._release_capacity_reservation(container_id)
        return SchedulerContainerCancellationResult(
            container_id=container_id,
            state_found=True,
            worker_id=state.worker_id,
            pending_request_removed=pending_request_removed,
            worker_stop_required=bool(
                state.worker_id and not terminal and not pending_request_removed
            ),
        )

    def _record_usage(self, request: SchedulerWorkerRequest, metric: UsageMetric) -> None:
        if self.usage is None or not request.workspace_id:
            return
        try:
            self.usage.record(
                id=usage_record_id(metric.value, request.workspace_id, request.container_id),
                workspace_id=request.workspace_id,
                resource_type="container",
                resource_id=request.container_id,
                metric=metric,
                quantity=1,
                unit=UsageUnit.Count,
                labels={
                    "workspace_id": request.workspace_id,
                    "stub_id": request.stub_id,
                    "gpu": request.gpu_type,
                },
                metadata={
                    "container_id": request.container_id,
                    "pool_selector": request.pool_selector,
                },
            )
        except Exception:
            LOGGER.debug("scheduling telemetry was not recorded", exc_info=True)
            return

    def dispatch_ready(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1,
    ) -> list[SchedulerContainerDispatchResult]:
        current_time = now or utc_now()
        claims = self.workers.claim_ready_container_requests(
            now=current_time,
            limit=limit,
            lease_seconds=self.claim_lease_seconds,
        )
        if not claims:
            return []

        placed_claims: list[SchedulerContainerRequestClaim] = []
        placement_failures: list[SchedulerContainerDispatchResult] = []
        for claim in claims:
            request = claim.request
            try:
                placed_claims.append(
                    SchedulerContainerRequestClaim(
                        request=self._resolve_capacity_owner(self.placement.place(request)),
                        token=claim.token,
                    )
                )
            except Exception as exc:
                reason = self._fail_request(request, str(exc), current_time)
                self._acknowledge(claim)
                placement_failures.append(
                    SchedulerContainerDispatchResult(
                        status=SchedulerContainerDispatchStatus.Failed,
                        container_id=request.container_id,
                        reason=reason,
                    )
                )
        claims = placed_claims
        cancelled = [
            claim
            for claim in claims
            if self.containers.is_container_cancelled(claim.request.container_id)
        ]
        claims = [claim for claim in claims if claim not in cancelled]
        for claim in cancelled:
            self._acknowledge(claim)
            self._release_capacity_reservation(claim.request.container_id, now=current_time)
        results = placement_failures + [
            SchedulerContainerDispatchResult(
                status=SchedulerContainerDispatchStatus.Cancelled,
                container_id=claim.request.container_id,
                reason="container request was cancelled",
            )
            for claim in cancelled
        ]
        if not claims:
            return results

        claims, reserved_dispatches = self._dispatch_registered_reservations(
            claims,
            now=current_time,
        )
        results.extend(reserved_dispatches)
        if not claims:
            return results
        requests = [claim.request for claim in claims]
        claims_by_request_id = {claim.request.container_id: claim for claim in claims}
        schedulable_workers = _schedulable_workers(self.workers)
        workers_by_id = {worker.worker_id: worker for worker in schedulable_workers}
        reserved_by_worker = (
            self.capacity_reservations.reserved_worker_capacity()
            if self.capacity_reservations is not None
            else {}
        )
        outcomes = {
            outcome.request_id: outcome
            for outcome in plan_scheduling_batch(
                [
                    _scheduling_request(
                        request,
                        provisionable=(
                            self.capacity_reservations.can_acquire(request)
                            if self.capacity_reservations is not None
                            else False
                        ),
                    )
                    for request in requests
                ],
                [
                    _worker_capacity(
                        worker,
                        reserved_capacity=reserved_by_worker.get(worker.worker_id),
                    )
                    for worker in schedulable_workers
                ],
                allow_provisioning=self.capacity_reservations is not None,
                worker_wait_delay=timedelta(seconds=self.requeue_delay_seconds),
            ).outcomes
        }
        for request in requests:
            claim = claims_by_request_id[request.container_id]
            outcome = outcomes[request.container_id]
            if outcome.decision is SchedulingDecision.Dispatch and outcome.worker_id:
                results.append(
                    self._dispatch(
                        claim,
                        workers_by_id[outcome.worker_id],
                        now=current_time,
                    )
                )
                continue
            if outcome.decision is SchedulingDecision.ProvisionWorker:
                results.append(self._acquire_capacity(claim, current_time))
                continue
            retry = self._plan_requeue(request, outcome, current_time)
            if retry.action is SchedulerRequeueAction.Fail:
                failure_reason = self._fail_request(
                    request,
                    _placement_failure_detail(
                        retry.reason.value,
                        request,
                        schedulable_workers,
                    ),
                    current_time,
                )
                self._acknowledge(claim)
                results.append(
                    SchedulerContainerDispatchResult(
                        status=SchedulerContainerDispatchStatus.Failed,
                        container_id=request.container_id,
                        worker_id=outcome.worker_id or "",
                        reason=failure_reason,
                    )
                )
                continue
            self._requeue(
                claim,
                current_time,
                delay_seconds=retry.delay_seconds,
                retry_count=(
                    request.retry_count
                    if outcome.decision is SchedulingDecision.WaitForWorker
                    else retry.next_retry_count
                ),
            )
            results.append(
                SchedulerContainerDispatchResult(
                    status=SchedulerContainerDispatchStatus.Waiting,
                    container_id=request.container_id,
                    worker_id=outcome.worker_id or "",
                    reason=retry.reason.value,
                )
            )
        return results

    def _resolve_capacity_owner(
        self,
        request: SchedulerWorkerRequest,
    ) -> SchedulerWorkerRequest:
        if self.capacity_reservations is None:
            return request
        return self.capacity_reservations.resolve_request(request)

    def _dispatch_registered_reservations(
        self,
        claims: list[SchedulerContainerRequestClaim],
        *,
        now: datetime,
    ) -> tuple[list[SchedulerContainerRequestClaim], list[SchedulerContainerDispatchResult]]:
        if self.capacity_reservations is None:
            return claims, []
        workers = {worker.worker_id: worker for worker in _schedulable_workers(self.workers)}
        remaining: list[SchedulerContainerRequestClaim] = []
        results: list[SchedulerContainerDispatchResult] = []
        for claim in claims:
            worker_id = self.capacity_reservations.registered_worker_id(claim.request.container_id)
            worker = workers.get(worker_id)
            if worker is None or worker.status is not SchedulerWorkerStatus.Available:
                remaining.append(claim)
                continue
            results.append(self._dispatch(claim, worker, now=now))
        return remaining, results

    def _acquire_capacity(
        self,
        claim: SchedulerContainerRequestClaim,
        current_time: datetime,
    ) -> SchedulerContainerDispatchResult:
        if self.capacity_reservations is None:
            raise RuntimeError("scheduler capacity reservation service was not injected")
        request = claim.request
        try:
            result = self.capacity_reservations.acquire(request, now=current_time)
        except CapacityReservationConflictError as exc:
            result = CapacityAcquisitionResult(
                status=CapacityAcquisitionStatus.ExistingPending,
                capacity_owner_id=request.capacity_owner_id,
                reservation_id=request.container_id,
                operation_id=request.container_id,
                retry_delay_seconds=self.requeue_delay_seconds,
                reason=f"capacity-owner mutation is in progress: {exc}",
            )
        except Exception as exc:
            # The reason reaches the caller as a task error, and a type name
            # alone cannot be acted on: it names neither the capacity owner nor
            # what the acquisition rejected. Keep the caller's contract and put
            # the exception where it can be read.
            LOGGER.exception(
                "capacity acquisition failed for container %s on capacity owner %s",
                request.container_id,
                request.capacity_owner_id,
            )
            result = CapacityAcquisitionResult(
                status=CapacityAcquisitionStatus.TemporarilyUnavailable,
                capacity_owner_id=request.capacity_owner_id,
                reservation_id=request.container_id,
                operation_id=request.container_id,
                retry_delay_seconds=DEFAULT_PROVISIONING_HANDOFF.total_seconds(),
                reason=f"capacity acquisition failed: {type(exc).__name__}: {exc}",
            )
        waiting = result.status in {
            CapacityAcquisitionStatus.ExistingPending,
            CapacityAcquisitionStatus.Requested,
        }
        synthetic_outcome = SchedulingOutcome(
            request_id=request.container_id,
            decision=(
                SchedulingDecision.WaitForWorker if waiting else SchedulingDecision.ProvisionWorker
            ),
            reason=result.reason,
            requeue_delay_seconds=max(
                result.retry_delay_seconds,
                self.requeue_delay_seconds,
            ),
        )
        retry = self._plan_requeue(request, synthetic_outcome, current_time)
        if retry.action is SchedulerRequeueAction.Fail:
            reason = self._fail_request(request, result.reason or retry.reason.value, current_time)
            self._acknowledge(claim)
            return SchedulerContainerDispatchResult(
                status=SchedulerContainerDispatchStatus.Failed,
                container_id=request.container_id,
                reason=reason,
            )
        self._requeue(
            claim,
            current_time,
            delay_seconds=max(retry.delay_seconds, result.retry_delay_seconds),
            retry_count=(request.retry_count if waiting else retry.next_retry_count),
        )
        return SchedulerContainerDispatchResult(
            status=SchedulerContainerDispatchStatus.Waiting,
            container_id=request.container_id,
            reason=result.reason or result.status.value,
        )

    def _reserve_quota(self, request: SchedulerWorkerRequest, now: datetime) -> str:
        if not _request_uses_quota(request):
            return ""
        gpu_count = gpu_count_for_capacity(
            request.gpu_type,
            request.gpu_request,
            request.gpu_count,
        )
        gpu_limit = request.gpu_limit if request.gpu_limit > 0 else max(gpu_count, 1_000_000)
        cpu_limit = (
            request.cpu_limit_millicores
            if request.cpu_limit_millicores > 0
            else max(request.cpu_millicores, 2_147_483_647)
        )
        decision = self.containers.reserve_concurrency(
            workspace_id=request.workspace_id,
            container_id=request.container_id,
            gpu_limit=gpu_limit,
            cpu_limit_millicores=cpu_limit,
            request_gpu_count=gpu_count,
            request_cpu_millicores=request.cpu_millicores,
            now=now,
        )
        status = decision.status
        if status is ConcurrencyReservationStatus.Ok:
            return ""
        reason = decision.reason or "workspace concurrency quota unavailable"
        return f"{status.value}: {reason}"

    def _plan_requeue(
        self,
        request: SchedulerWorkerRequest,
        outcome: SchedulingOutcome,
        current_time: datetime,
    ) -> SchedulerRequeuePlan:
        processing_interval = timedelta(seconds=max(self.requeue_delay_seconds, 0.0))
        max_schedule_duration = timedelta(seconds=max(self.max_retry_age_seconds, 0.0))
        if outcome.decision is SchedulingDecision.WaitForWorker:
            return plan_worker_wait_requeue(
                request_created_at=request.timestamp,
                now=current_time,
                delay=timedelta(
                    seconds=max(outcome.requeue_delay_seconds, self.requeue_delay_seconds, 0.0)
                ),
                processing_interval=processing_interval,
                max_schedule_duration=max_schedule_duration,
            )
        return plan_retry_soon(
            retry_count=request.retry_count,
            request_created_at=request.timestamp,
            now=current_time,
            max_retry_count=self.max_retry_count,
            processing_interval=processing_interval,
            max_schedule_duration=max_schedule_duration,
        )

    def _dispatch(
        self,
        claim: SchedulerContainerRequestClaim,
        worker: SchedulerWorkerRecord,
        *,
        now: datetime,
    ) -> SchedulerContainerDispatchResult:
        capacity_owner_id = worker.capacity_owner_id
        if not capacity_owner_id:
            return self._dispatch_claim(claim, worker, now=now)
        if self.capacity_reservations is None:
            return self._requeue_capacity_owner_dispatch(
                claim,
                worker_id=worker.worker_id,
                now=now,
                reason="capacity-owner mutation coordination is unavailable",
            )

        dispatch_result: SchedulerContainerDispatchResult | None = None
        try:
            # No caller enters final dispatch while holding an owner lease. Reserved
            # allocations are only read here and consumed by the atomic worker-queue
            # commit below, so this is the single lease for both reserved and direct
            # dispatch paths.
            with self.capacity_reservations.mutation_lock(capacity_owner_id):
                current_worker = self.workers.get_worker(worker.worker_id)
                if (
                    current_worker is None
                    or current_worker.status is not SchedulerWorkerStatus.Available
                    or current_worker.capacity_owner_id != capacity_owner_id
                    or (
                        claim.request.capacity_owner_id
                        and claim.request.capacity_owner_id != capacity_owner_id
                    )
                ):
                    dispatch_result = self._requeue_capacity_owner_dispatch(
                        claim,
                        worker_id=worker.worker_id,
                        now=now,
                        reason="capacity-owner worker changed before final dispatch",
                    )
                else:
                    dispatch_result = self._dispatch_claim(claim, current_worker, now=now)
        except CapacityReservationConflictError as exc:
            # A lease can report loss while leaving a completed body. In that case
            # _dispatch_claim has already either committed the worker request or
            # preserved it through its normal rollback; never process the claim twice.
            if dispatch_result is not None:
                return dispatch_result
            return self._requeue_capacity_owner_dispatch(
                claim,
                worker_id=worker.worker_id,
                now=now,
                reason=f"capacity-owner mutation is in progress: {exc}",
            )
        if dispatch_result is not None:
            return dispatch_result
        return self._requeue_capacity_owner_dispatch(
            claim,
            worker_id=worker.worker_id,
            now=now,
            reason="capacity-owner mutation lease closed before final dispatch",
        )

    def _requeue_capacity_owner_dispatch(
        self,
        claim: SchedulerContainerRequestClaim,
        *,
        worker_id: str,
        now: datetime,
        reason: str,
    ) -> SchedulerContainerDispatchResult:
        try:
            self._requeue(
                claim,
                now,
                retry_count=claim.request.retry_count,
            )
        except Exception as exc:
            return SchedulerContainerDispatchResult(
                status=SchedulerContainerDispatchStatus.Error,
                container_id=claim.request.container_id,
                worker_id=worker_id,
                reason=f"{reason}; failed to preserve scheduler request: {exc}",
            )
        return SchedulerContainerDispatchResult(
            status=SchedulerContainerDispatchStatus.Waiting,
            container_id=claim.request.container_id,
            worker_id=worker_id,
            reason=reason,
        )

    def _dispatch_claim(
        self,
        claim: SchedulerContainerRequestClaim,
        worker: SchedulerWorkerRecord,
        *,
        now: datetime,
    ) -> SchedulerContainerDispatchResult:
        request = claim.request
        worker_id = worker.worker_id
        reserved_capacity = self._reserved_capacity_for_request(request)
        capacity_allocation = (
            self.capacity_reservations.prepare_dispatch(
                request.container_id,
                worker,
                now=now,
            )
            if self.capacity_reservations is not None
            else None
        )
        assigned_state = _container_state(request, worker_id=worker_id)
        if reserved_capacity is not None:
            assigned_state = assigned_state.model_copy(
                update={
                    "cpu_millicores": reserved_capacity.cpu_millicores,
                    "memory_mib": reserved_capacity.memory_mib,
                    "gpu_count": reserved_capacity.gpu_count,
                }
            )
        try:
            recorded_state = self.containers.set_container_state(assigned_state)
            if recorded_state.status is SchedulerContainerStatus.Stopping:
                raise RuntimeError(f"container request {request.container_id} was cancelled")
            if request.record_runtime_assignment:
                self.assignments.assign_runtime(
                    container_id=request.container_id,
                    workspace_id=request.workspace_id,
                    runtime_worker_id=worker.worker_id,
                    runtime_machine_id=worker.machine_id,
                    compute_worker_id=(worker.worker_id if worker.private_worker else None),
                    compute_machine_id=(worker.machine_id if worker.private_worker else None),
                )
            self.workers.dispatch_claimed_container_request(
                worker_id,
                claim,
                reserved_capacity=reserved_capacity,
                capacity_allocation=capacity_allocation,
                now=now,
            )
        except ContainerRequestCancelledError as exc:
            if request.record_runtime_assignment:
                self.assignments.clear_runtime_assignment(
                    container_id=request.container_id,
                    runtime_worker_id=worker_id,
                )
            self.containers.delete_container_state(request.container_id)
            self._release_capacity_reservation(request.container_id, now=now)
            return SchedulerContainerDispatchResult(
                status=SchedulerContainerDispatchStatus.Cancelled,
                container_id=request.container_id,
                worker_id=worker_id,
                reason=str(exc),
            )
        except ContainerRequestClaimNotOwnedError as exc:
            if request.record_runtime_assignment:
                self.assignments.clear_runtime_assignment(
                    container_id=request.container_id,
                    runtime_worker_id=worker_id,
                )
            return SchedulerContainerDispatchResult(
                status=SchedulerContainerDispatchStatus.Waiting,
                container_id=request.container_id,
                reason=str(exc),
            )
        except Exception as exc:
            if request.record_runtime_assignment:
                try:
                    self.assignments.clear_runtime_assignment(
                        container_id=request.container_id,
                        runtime_worker_id=worker_id,
                    )
                except Exception as cleanup_exc:
                    exc = RuntimeError(f"{exc}; failed to clear runtime assignment: {cleanup_exc}")
            if not self.containers.is_container_cancelled(request.container_id):
                self.containers.set_container_state(_container_state(request))
            self._requeue(claim, now)
            return SchedulerContainerDispatchResult(
                status=SchedulerContainerDispatchStatus.Error,
                container_id=request.container_id,
                worker_id=worker_id,
                reason=str(exc),
            )
        self._record_dispatch_lifecycle(request, claimed_at=now)
        return SchedulerContainerDispatchResult(
            status=SchedulerContainerDispatchStatus.Dispatched,
            container_id=request.container_id,
            worker_id=worker_id,
            reason="container request dispatched to worker",
        )

    def _record_dispatch_lifecycle(
        self,
        request: SchedulerWorkerRequest,
        *,
        claimed_at: datetime,
    ) -> None:
        duration_ms = max(int((claimed_at - request.timestamp).total_seconds() * 1000), 0)
        try:
            self.lifecycle_events.append_event(
                EventRecordType.ContainerLifecycle,
                {
                    "id": "scheduler",
                    "event_id": "scheduler",
                    "container_id": request.container_id,
                    "stub_id": request.stub_id,
                    "workspace_id": request.workspace_id,
                    "start_time": request.timestamp,
                    "end_time": claimed_at,
                    "duration_ms": duration_ms,
                    "success": True,
                    "attrs": {"phase": "scheduler"},
                },
            )
        except Exception:
            LOGGER.warning(
                "scheduler dispatch lifecycle publication failed",
                exc_info=True,
                extra={"container_id": request.container_id},
            )

    def _reserved_capacity_for_request(
        self,
        request: SchedulerWorkerRequest,
    ) -> WorkerReservedCapacity:
        return WorkerReservedCapacity(
            cpu_millicores=request.cpu_millicores,
            memory_mib=capacity_memory_mib(request.memory_mib),
            gpu_count=gpu_count_for_capacity(
                request.gpu_type,
                request.gpu_request,
                request.gpu_count,
            ),
        )

    def _fail_request(
        self,
        request: SchedulerWorkerRequest,
        reason: str,
        now: datetime,
    ) -> str:
        self.containers.set_container_state(
            _container_state(
                request,
                status=SchedulerContainerStatus.Failed,
                scheduled_at=now,
                failure_reason=reason,
            )
        )
        if request.record_runtime_assignment:
            try:
                self.failure_handler.mark_scheduling_failed(request, reason, now=now)
            except Exception as exc:  # pragma: no cover - defensive callback boundary
                return f"{reason}; failed to sync runtime state: {type(exc).__name__}"
        if _request_uses_quota(request):
            try:
                self.containers.release_concurrency_reservation(
                    request.workspace_id,
                    request.container_id,
                    now=now,
                )
            except Exception as exc:
                reason = (
                    f"{reason}; failed to release concurrency reservation: {type(exc).__name__}"
                )
        self._release_capacity_reservation(request.container_id, now=now)
        return reason

    def _release_capacity_reservation(
        self,
        container_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        if self.capacity_reservations is None:
            return
        try:
            self.capacity_reservations.release_request(
                container_id,
                workers=tuple(_schedulable_workers(self.workers)),
                now=now,
            )
        except Exception:
            LOGGER.warning(
                "capacity reservation release failed; reconciliation will retry",
                exc_info=True,
                extra={"container_id": container_id},
            )

    def _requeue(
        self,
        claim: SchedulerContainerRequestClaim,
        now: datetime,
        *,
        delay_seconds: float | None = None,
        retry_count: int | None = None,
    ) -> None:
        request = claim.request
        delay = self.requeue_delay_seconds if delay_seconds is None else delay_seconds
        ready_at = now + timedelta(seconds=max(delay, 0.0))
        requeued = self.workers.requeue_container_request(
            claim,
            request.model_copy(
                update={
                    "retry_count": (request.retry_count + 1 if retry_count is None else retry_count)
                }
            ),
            ready_at=ready_at,
        )
        if not requeued and not self.containers.is_container_cancelled(request.container_id):
            raise RuntimeError(f"failed to requeue scheduler request {request.container_id}")

    def _acknowledge(self, claim: SchedulerContainerRequestClaim) -> None:
        if not self.workers.acknowledge_container_request(claim):
            raise RuntimeError(
                f"scheduler request claim is no longer owned: {claim.request.container_id}"
            )


def _container_state(
    request: SchedulerWorkerRequest,
    *,
    worker_id: str = "",
    scheduled_at: datetime | None = None,
    status: SchedulerContainerStatus = SchedulerContainerStatus.Pending,
    failure_reason: str = "",
) -> SchedulerContainerState:
    is_image_build = request.payload.get("kind") == "image-build"
    return SchedulerContainerState(
        container_id=request.container_id,
        stub_id=request.stub_id,
        workspace_id=request.workspace_id,
        worker_id=worker_id,
        status=status,
        scheduled_at=scheduled_at or utc_now(),
        gpu_type=request.gpu_type,
        gpu_count=gpu_count_for_capacity(
            request.gpu_type,
            request.gpu_request,
            request.gpu_count,
        ),
        cpu_millicores=request.cpu_millicores,
        memory_mib=request.memory_mib,
        image_build_id=str(request.payload.get("build_id") or "") if is_image_build else "",
        image_id=str(request.payload.get("image_id") or ""),
        image_build_upload_capability=(
            str(request.payload.get("archive_upload_capability") or "") if is_image_build else ""
        ),
        failure_reason=failure_reason,
    )


def _scheduling_request(
    request: SchedulerWorkerRequest,
    *,
    provisionable: bool = True,
) -> SchedulingRequest:
    memory_mib = capacity_memory_mib(request.memory_mib)
    cpu = request.cpu_millicores / 1000
    gpu_count = gpu_count_for_capacity(
        request.gpu_type,
        request.gpu_request,
        request.gpu_count,
    )
    return SchedulingRequest(
        id=request.container_id,
        capacity_owner_id=request.capacity_owner_id,
        queue=request.stub_id or "containers",
        payload=request.payload,
        cpu=cpu,
        memory_mib=memory_mib,
        gpu_count=gpu_count,
        gpu_type=request.gpu_type,
        gpu_request=list(request.gpu_request),
        pool_selector=request.pool_selector,
        runtime_class=request.runtime_class,
        docker_enabled=request.docker_enabled,
        preemptible=request.preemptible,
        provisionable=provisionable,
        retry_count=request.retry_count,
        created_at=request.timestamp,
    )


def _placement_failure_detail(
    reason: str,
    request: SchedulerWorkerRequest,
    workers: list[SchedulerWorkerRecord],
) -> str:
    """Explain an unplaceable request instead of reporting a bare retry reason.

    A request that never finds a worker previously failed with only "retry-limit",
    which says nothing about whether capacity was missing or merely mismatched.
    """
    selector = request.pool_selector or "<none>"
    if not workers:
        return f"{reason}: no schedulable workers (pool selector {selector})"
    scheduling = _scheduling_request(request, provisionable=False)
    rejections = [
        f"{worker.worker_id[:8]} in {worker.pool_name!r}: {detail}"
        for worker in workers[:3]
        if (detail := _worker_capacity(worker).fit_rejection(scheduling))
    ]
    if not rejections:
        return f"{reason} (pool selector {selector})"
    return f"{reason}: pool selector {selector}; " + "; ".join(rejections)


def _worker_capacity(
    worker: SchedulerWorkerRecord,
    *,
    reserved_capacity: WorkerReservedCapacity | None = None,
) -> WorkerCapacity:
    reserved = reserved_capacity or WorkerReservedCapacity()
    return WorkerCapacity(
        worker_id=worker.worker_id,
        pool=worker.pool_name,
        capacity_owner_id=worker.capacity_owner_id,
        gpu_type=worker.gpu_type,
        runtime_class=worker.runtime_class,
        runtime_classes=list(worker.runtime_classes),
        requires_pool_selector=worker.requires_pool_selector,
        preemptible=worker.preemptible,
        free_cpu=max(worker.free_cpu_millicores - reserved.cpu_millicores, 0) / 1000,
        free_memory_mib=max(worker.free_memory_mib - reserved.memory_mib, 0),
        free_gpu=max(worker.free_gpu_count - reserved.gpu_count, 0),
        total_cpu=worker.total_cpu_millicores / 1000,
        total_memory_mib=worker.total_memory_mib,
        total_gpu=worker.total_gpu_count,
        pending=worker.status is SchedulerWorkerStatus.Pending,
    )


def _schedulable_workers(
    repository: SchedulerContainerWorkerRepository,
) -> list[SchedulerWorkerRecord]:
    return [
        worker
        for worker in repository.list_workers()
        if worker.status in {SchedulerWorkerStatus.Available, SchedulerWorkerStatus.Pending}
    ]


def _request_uses_quota(request: SchedulerWorkerRequest) -> bool:
    return request.gpu_limit > 0 or request.cpu_limit_millicores > 0
