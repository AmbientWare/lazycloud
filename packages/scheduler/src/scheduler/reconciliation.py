from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from compute.projection import PrivateUnitState
from database.records.apps import AppRecord
from pydantic import Field, JsonValue
from shared.containers import ContainerRecord
from shared.contracts import ContractModel
from shared.cron import CronJobRun
from shared.events import EventLevel
from shared.scheduling import WorkerRemovalResult
from shared.tasks import Task
from shared.worker_events import WORKER_POOL_DRAIN_DECISION_ACTION

from scheduler.agent_pool import AgentPoolReconcileResult
from scheduler.autoscaling import AutoscaleResult
from scheduler.capacity_reservations import CapacityProvisioningReservation
from scheduler.containers import SchedulerContainerDispatchResult
from scheduler.fleet import WorkerPoolStateSnapshot
from scheduler.pool_drain import (
    WorkerPoolDrainAction,
    WorkerPoolDrainResult,
)
from scheduler.preemption import WorkerPreemptionResult
from scheduler.services import FleetServices

LOGGER = logging.getLogger(__name__)


WORKER_POOL_DRAIN_SOURCE = "worker_pool.drain"


CONTAINER_EXPIRY_LOCK_TTL_SECONDS = 30


BILLING_ENFORCEMENT_LOCK_TTL_SECONDS = 30


"""Long enough for one bounded pass, short enough that a scheduler killed
mid-sweep does not leave unfunded compute running for a minute."""


AUTOSCALING_TARGET_CLAIM_LEASE_SECONDS = 30.0


AUTOSCALING_TARGET_RECONCILE_INTERVAL_SECONDS = 1.0


DEFAULT_AUTOSCALING_RECONCILE_LIMIT = 500


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
