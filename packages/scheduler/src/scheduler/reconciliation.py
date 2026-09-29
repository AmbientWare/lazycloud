from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from compute.projection import PrivateUnitState
from compute.state import RedisComputeStateRepository
from coordination.redis_client import RedisClient
from coordination.wake_signal import WakeSignalWaiter
from database.records.apps import AppRecord, StubRecord
from database.repositories.execution import (
    CronJobRunCursor,
)
from pydantic import Field, JsonValue
from shared.containers import ContainerRecord
from shared.contracts import ContractModel
from shared.cron import CronJobRun, next_cron_run
from shared.errors import InvalidInputError
from shared.events import EventLevel
from shared.http.functions import FunctionInvokeBody, FunctionInvokeResponse
from shared.scheduling import WorkerRemovalResult
from shared.tasks import Task
from shared.worker_events import (
    WORKER_POOL_DRAIN_DECISION_ACTION,
)

from scheduler.agent_pool import (
    AgentPoolConfig,
    AgentPoolReconcileResult,
    SchedulerAgentPoolService,
)
from scheduler.autoscaling import (
    AutoscaleResult,
    AutoscalingDriver,
)
from scheduler.capacity_reservations import (
    CapacityProvisioningReservation,
    CapacityReservationService,
)
from scheduler.containers import (
    SchedulerContainerDispatchResult,
    SchedulerContainerRequestService,
)
from scheduler.fleet import WorkerPoolStateSnapshot
from scheduler.pool_drain import (
    WorkerPoolDrainAction,
    WorkerPoolDrainResult,
    WorkerPoolDrainService,
)
from scheduler.pool_state import SchedulerPoolStateService
from scheduler.preemption import (
    SchedulerCapacityInterruptionService,
    WorkerPreemptionResult,
)
from scheduler.reserves import FleetConsolidationService
from scheduler.services import FleetServices, SchedulerAutoscalingTargetService

LOGGER = logging.getLogger(__name__)


WORKER_POOL_DRAIN_SOURCE = "worker_pool.drain"


CRON_JOB_LOCK_TTL_SECONDS = 10


CONTAINER_EXPIRY_LOCK_TTL_SECONDS = 30


BILLING_ENFORCEMENT_LOCK_TTL_SECONDS = 30


"""Long enough for one bounded pass, short enough that a scheduler killed
mid-sweep does not leave unfunded compute running for a minute."""


AUTOSCALING_TARGET_CLAIM_LEASE_SECONDS = 30.0


AUTOSCALING_TARGET_RECONCILE_INTERVAL_SECONDS = 1.0


DEFAULT_AUTOSCALING_RECONCILE_LIMIT = 500


CONTAINER_DISPATCH_SWEEP_INTERVAL_SECONDS = 1.0


MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS = 60.0


ORPHANED_CONTAINER_RECONCILE_INTERVAL_SECONDS = 30.0


CAPACITY_PASS_SLOW_SECONDS = 2.0


"""A capacity pass at least this long logs the seconds each of its steps took."""


ORPHANED_CONTAINER_CONFIRMATION_SECONDS = 60.0


ORPHANED_CONTAINER_FAILURE_REASON = (
    "container execution state was lost before the workload reached a recoverable runtime"
)


def _next_autoscaling_target_reconcile(
    result: AutoscaleResult | None,
    *,
    now: datetime,
) -> datetime | None:
    if result is None:
        return None
    if (
        not result.lock_acquired
        or result.current_containers > 0
        or result.pending_containers > 0
        or result.desired_containers > 0
        or result.signal_value > 0
        or result.actions
    ):
        return now + timedelta(seconds=AUTOSCALING_TARGET_RECONCILE_INTERVAL_SECONDS)
    return None


def _function_cron_job_lock_key(stub_id: str) -> str:
    return f"function:cron_jobs_lock:{stub_id}"


@runtime_checkable
class WorkerCleanupRepository(Protocol):
    def cleanup_missing_workers(
        self,
        *,
        now: datetime | None = None,
    ) -> list[WorkerRemovalResult]: ...


class OrphanedContainerNetworkRepository(Protocol):
    def remove_container_ips(self, container_id: str) -> None: ...


class OrphanedContainerConfirmationRepository(Protocol):
    def first_observed_at(
        self,
        container_id: str,
        *,
        now: datetime,
        ttl_seconds: int,
    ) -> datetime: ...

    def claim_confirmed(self, container_id: str) -> bool: ...

    def forget(self, container_id: str) -> None: ...


class SchedulerVolumeMeteringBatch(Protocol):
    @property
    def metered_count(self) -> int: ...

    @property
    def failure_count(self) -> int: ...


class SchedulerStorageAccessService(Protocol):
    def reconcile(self) -> int: ...


class SchedulerVolumeMeteringService(Protocol):
    def reconcile_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> SchedulerVolumeMeteringBatch: ...


class SchedulerVolumeDeletionService(Protocol):
    def reconcile_due(self, *, now: datetime | None = None, limit: int = 100) -> None: ...


class SchedulerDiskDeletionService(Protocol):
    def reconcile_due(self, *, now: datetime | None = None, limit: int = 100) -> None: ...


class SchedulerDiskVolumeService(Protocol):
    def reconcile_due(self, *, now: datetime | None = None, limit: int = 100) -> None: ...


class SchedulerMeterEventBatch(Protocol):
    @property
    def sent_count(self) -> int: ...

    @property
    def retried_count(self) -> int: ...

    @property
    def abandoned_count(self) -> int: ...


class SchedulerAbandonedMeterEvents(Protocol):
    @property
    def count(self) -> int: ...

    @property
    def value_nanos(self) -> int: ...


class SchedulerEmailDrainResult(Protocol):
    @property
    def sent_count(self) -> int: ...

    @property
    def retried_count(self) -> int: ...

    @property
    def abandoned_count(self) -> int: ...


class SchedulerEmailOutboxService(Protocol):
    """The sweep that delivers what a request already committed to sending."""

    def drain(self, *, now: datetime | None = None) -> SchedulerEmailDrainResult: ...

    def abandoned_backlog(self) -> int: ...

    def redact(self, *, now: datetime | None = None, limit: int = 1_000) -> int: ...


class SchedulerMeterOutboxService(Protocol):
    """The sweep that hands the provider what the pricer already owed it."""

    def drain(self, *, now: datetime | None = None) -> SchedulerMeterEventBatch: ...

    def abandoned_backlog(self) -> SchedulerAbandonedMeterEvents: ...


class SchedulerPlanChangeBatch(Protocol):
    @property
    def applied_count(self) -> int: ...

    @property
    def not_applied_count(self) -> int: ...

    @property
    def retried_count(self) -> int: ...

    @property
    def abandoned_count(self) -> int: ...

    @property
    def open_count(self) -> int: ...


class SchedulerPlanChangeService(Protocol):
    """The sweep that finishes plan changes whose outcome nobody recorded."""

    def settle_open(self, *, now: datetime | None = None) -> SchedulerPlanChangeBatch: ...


class SchedulerBillingReconciliationBatch(Protocol):
    @property
    def accounts_checked(self) -> int: ...

    @property
    def divergent_count(self) -> int: ...

    @property
    def unreachable_count(self) -> int: ...


class SchedulerBillingReconciliationService(Protocol):
    """The pass that reports where the provider and this platform disagree."""

    def reconcile(self, *, now: datetime | None = None) -> SchedulerBillingReconciliationBatch: ...


class SchedulerBillingPaymentsService(Protocol):
    def maintain(self, *, now: datetime | None = None) -> None: ...


class SchedulerBillingEnforcementBatch(Protocol):
    @property
    def accounts_checked(self) -> int: ...

    @property
    def unfunded_count(self) -> int: ...

    @property
    def stopped_count(self) -> int: ...

    @property
    def failed_count(self) -> int: ...


class SchedulerBillingEnforcementService(Protocol):
    """The pass that stops compute nobody can be billed for."""

    def enforce(self, *, now: datetime | None = None) -> SchedulerBillingEnforcementBatch: ...


@dataclass(frozen=True, slots=True)
class _MeterEventSweep:
    """What one tick of the outbox did, and what it left behind.

    The first three are the tick's own work and the last two are the standing
    backlog, which is read whether or not the sending worked: a provider outage
    is exactly when charges are given up on, and a gauge that went quiet for the
    duration of one would report nothing during the failure it exists for.
    """

    sent_count: int = 0
    retried_count: int = 0
    abandoned_count: int = 0
    abandoned_outstanding_count: int = 0
    abandoned_outstanding_nanos: int = 0


@dataclass(frozen=True, slots=True)
class _NoPlanChanges:
    applied_count: int = 0
    not_applied_count: int = 0
    retried_count: int = 0
    abandoned_count: int = 0
    open_count: int = 0


@dataclass(frozen=True, slots=True)
class _NoBillingReconciliation:
    accounts_checked: int = 0
    divergent_count: int = 0
    unreachable_count: int = 0


@dataclass(frozen=True, slots=True)
class _NoBillingEnforcement:
    accounts_checked: int = 0
    unfunded_count: int = 0
    stopped_count: int = 0
    failed_count: int = 0


_NO_METER_EVENT_SWEEP = _MeterEventSweep()


_NO_PLAN_CHANGES = _NoPlanChanges()


_NO_BILLING_RECONCILIATION = _NoBillingReconciliation()


_NO_BILLING_ENFORCEMENT = _NoBillingEnforcement()


class SchedulerRetentionBatch(Protocol):
    @property
    def removed(self) -> int: ...


class SchedulerRetentionService(Protocol):
    def reconcile(self, *, now: datetime | None = None) -> SchedulerRetentionBatch: ...


class SchedulerCustomDomainService(Protocol):
    def reconcile_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> int: ...


class ScheduledFunctionControl(Protocol):
    def expire_timed_out_tasks(self, *, now: datetime, limit: int = 100) -> None: ...

    def schedule_due_retries(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[Task]: ...

    def function_invoke(
        self, request: FunctionInvokeBody, *, stub: StubRecord
    ) -> FunctionInvokeResponse: ...


class SchedulerPreemptionRecovery(Protocol):
    def recover_unsettled(self, *, limit: int = 100) -> list[str]: ...


def next_run_after(expression: str, now: datetime | None = None) -> datetime:
    return next_cron_run(expression, now)


class CronJobRunDraft(ContractModel):
    workspace_id: str
    cron_job: str
    enqueued: bool
    task_id: str | None = None
    reason: str | None = None


class CronJobRunCursorPayload(ContractModel):
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class SchedulerCronJobRunPage:
    data: tuple[CronJobRun, ...]
    next: str = ""


def _encode_cron_job_run_cursor(cursor: CronJobRunCursor | None) -> str:
    if cursor is None:
        return ""
    payload = CronJobRunCursorPayload(
        created_at=cursor.created_at,
        id=cursor.id,
    ).model_dump_json()
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cron_job_run_cursor(value: str | None) -> CronJobRunCursor | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = CronJobRunCursorPayload.model_validate_json(
            base64.urlsafe_b64decode(padded.encode())
        )
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidInputError("invalid cron job run cursor") from exc
    return CronJobRunCursor(created_at=payload.created_at, id=payload.id)


class SchedulerBuildSubmissions(Protocol):
    def drain(self, *, limit: int = 16) -> int: ...

    def recover(self, *, limit: int = 100) -> int: ...

    def cleanup(self, *, limit: int = 16) -> None: ...


@dataclass(frozen=True, slots=True)
class SchedulerWorkloadControls:
    image_builds: SchedulerBuildSubmissions | None = None
    containers: SchedulerContainerRequestService | None = None
    dispatch_wake: WakeSignalWaiter | None = None
    placement_wake: WakeSignalWaiter | None = None
    capacity_wake: WakeSignalWaiter | None = None
    function_autoscaler: AutoscalingDriver | None = None
    endpoints: AutoscalingDriver | None = None
    pods: AutoscalingDriver | None = None
    functions: ScheduledFunctionControl | None = None
    preemption_recovery: SchedulerPreemptionRecovery | None = None
    autoscaling_targets: SchedulerAutoscalingTargetService | None = None


@dataclass(frozen=True, slots=True)
class SchedulerStateStores:
    compute: RedisComputeStateRepository | None = None
    pools: SchedulerPoolStateService | None = None
    orphaned_container_networks: OrphanedContainerNetworkRepository | None = None
    orphaned_container_confirmations: OrphanedContainerConfirmationRepository | None = None
    cron_job_locks: RedisClient | None = None


@dataclass(frozen=True, slots=True)
class SchedulerCapacityControls:
    agent_pools: SchedulerAgentPoolService | None = None
    agent_pool_configs: Callable[[], list[AgentPoolConfig]] | None = None
    capacity_reservations: CapacityReservationService | None = None
    worker_pool_drain: WorkerPoolDrainService | None = None
    capacity_interruptions: SchedulerCapacityInterruptionService | None = None
    consolidation: FleetConsolidationService | None = None


@dataclass(frozen=True, slots=True)
class SchedulerMaintenanceControls:
    billing_payments: SchedulerBillingPaymentsService | None = None
    storage_access: SchedulerStorageAccessService | None = None
    volume_metering: SchedulerVolumeMeteringService | None = None
    volume_deletion: SchedulerVolumeDeletionService | None = None
    disk_deletion: SchedulerDiskDeletionService | None = None
    disk_volumes: SchedulerDiskVolumeService | None = None
    meter_outbox: SchedulerMeterOutboxService | None = None
    email_outbox: SchedulerEmailOutboxService | None = None
    plan_changes: SchedulerPlanChangeService | None = None
    billing_reconciliation: SchedulerBillingReconciliationService | None = None
    billing_enforcement: SchedulerBillingEnforcementService | None = None
    retention: SchedulerRetentionService | None = None
    custom_domains: SchedulerCustomDomainService | None = None


class SchedulerRunResult(ContractModel):
    storage_access_observed: int | None = None
    storage_access_failures: int = 0
    app_lifecycle_reconciliations: list[AppRecord] = Field(default_factory=list)
    cron_job_runs: list[CronJobRun] = Field(default_factory=list)
    function_retries: list[Task] = Field(default_factory=list)
    agent_pool_reconciliations: list[AgentPoolReconcileResult] = Field(default_factory=list)
    function_autoscaling: list[AutoscaleResult] = Field(default_factory=list)
    endpoint_autoscaling: list[AutoscaleResult] = Field(default_factory=list)
    pod_autoscaling: list[AutoscaleResult] = Field(default_factory=list)
    expired_containers: list[ContainerRecord] = Field(default_factory=list)
    pool_states: dict[str, WorkerPoolStateSnapshot] = Field(default_factory=dict)
    capacity_reservations: list[CapacityProvisioningReservation] = Field(default_factory=list)
    capacity_interruptions: list[WorkerPreemptionResult] = Field(default_factory=list)
    managed_compute_reconciliations: list[PrivateUnitState] = Field(default_factory=list)
    worker_pool_drains: list[WorkerPoolDrainResult] = Field(default_factory=list)
    container_dispatches: list[SchedulerContainerDispatchResult] = Field(default_factory=list)
    orphaned_containers_failed: list[str] = Field(default_factory=list)
    settled_preemptions: list[str] = Field(default_factory=list)
    worker_cleanups: list[WorkerRemovalResult] = Field(default_factory=list)
    capacity_demand_contended: list[str] = Field(default_factory=list)
    expired_tokens_pruned: int = 0
    events_pruned: int = 0
    volume_metering_count: int = 0
    volume_metering_failure_count: int = 0
    meter_events_sent_count: int = 0
    meter_events_retried_count: int = 0
    meter_events_abandoned_count: int = 0
    meter_events_abandoned_outstanding_count: int = 0
    meter_events_abandoned_outstanding_nanos: int = 0
    plan_changes_applied_count: int = 0
    plan_changes_not_applied_count: int = 0
    plan_changes_retried_count: int = 0
    plan_changes_abandoned_count: int = 0
    plan_changes_open_count: int = 0
    billing_reconcile_checked_count: int = 0
    billing_reconcile_divergent_count: int = 0
    billing_reconcile_failure_count: int = 0
    billing_enforcement_unfunded_count: int = 0
    billing_enforcement_stopped_count: int = 0
    billing_enforcement_failure_count: int = 0
    objects_removed: int = 0
    retention_failure_count: int = 0


def _merge_run_results(*results: SchedulerRunResult) -> SchedulerRunResult:
    """One sweep's worth of work, assembled from the passes that did it.

    Each pass fills only its own fields and leaves the rest at their defaults,
    so merging is taking what was set. That holds because no field is written by
    two passes: a pass whose real answer happens to equal the default
    contributes the same value the merged result would carry anyway.
    """

    merged: dict[str, object] = {}
    for result in results:
        merged.update(result.model_dump(exclude_defaults=True))
    return SchedulerRunResult.model_validate(merged)


WORKER_POOL_DRAIN_LOG_INTERVAL_SECONDS = 300.0


def _log_worker_pool_drain_decisions(
    results: list[WorkerPoolDrainResult],
    *,
    logged_at: dict[str, datetime],
    now: datetime,
) -> None:
    """Say what each pool decided, including when it decides the same thing."""

    for result in results:
        pool = result.placement.key
        last = logged_at.get(result.capacity_owner_id)
        acted = result.action is not WorkerPoolDrainAction.None_
        due = last is None or (now - last).total_seconds() >= (
            WORKER_POOL_DRAIN_LOG_INTERVAL_SECONDS
        )
        if not acted and not due:
            continue
        logged_at[result.capacity_owner_id] = now
        LOGGER.info(
            "worker-pool drain for %s (%s): action=%s reason=%s desired=%d observed=%d%s",
            pool,
            result.capacity_owner_id,
            result.action.value,
            result.reason or "-",
            result.desired_replicas,
            result.observed_replicas,
            f" error={result.error}" if result.error else "",
        )


def _record_worker_pool_drain_observability(
    services: FleetServices,
    results: list[WorkerPoolDrainResult],
    *,
    event_signatures: dict[str, tuple[str, ...]],
) -> None:
    for result in results:
        if result.lock_acquired:
            signature_key = result.capacity_owner_id
            signature = (
                result.action.value,
                result.reason,
                result.error,
                str(result.desired_replicas),
                str(result.observed_replicas),
            )
            if (
                result.action is not WorkerPoolDrainAction.None_
                or result.drained_worker_ids
                or event_signatures.get(signature_key) != signature
            ):
                data: dict[str, JsonValue] = {
                    "source": WORKER_POOL_DRAIN_SOURCE,
                    "placement": result.placement.key,
                    "capacity_owner_id": result.capacity_owner_id,
                    "action": result.action.value,
                    "machine_id": result.machine_id,
                    "desired_replicas": result.desired_replicas,
                    "observed_replicas": result.observed_replicas,
                    "drained_worker_ids": list(result.drained_worker_ids),
                    "reason": result.reason,
                    "lock_acquired": result.lock_acquired,
                    "error": result.error,
                }
                services.events.emit(
                    WORKER_POOL_DRAIN_DECISION_ACTION,
                    resource_type="worker_pool",
                    resource_id=result.capacity_owner_id,
                    message="worker-pool drain selected desired capacity",
                    level=EventLevel.Warning if result.error else EventLevel.Info,
                    data=data,
                )
            event_signatures[signature_key] = signature
        labels = {"source": WORKER_POOL_DRAIN_SOURCE, "placement": str(result.placement)}
        services.metrics.increment(
            "worker_pool_drain_decisions_total",
            labels={
                **labels,
                "action": result.action.value,
                "reason": result.reason,
            },
        )
        services.metrics.set_gauge(
            "worker_pool_desired_replicas",
            result.desired_replicas,
            labels=labels,
        )
        services.metrics.set_gauge(
            "worker_pool_observed_replicas",
            result.observed_replicas,
            labels=labels,
        )
        services.metrics.set_gauge(
            "worker_pool_drained_workers",
            len(result.drained_worker_ids),
            labels=labels,
        )
        if result.error:
            services.metrics.increment(
                "worker_pool_errors_total",
                labels=labels,
            )
